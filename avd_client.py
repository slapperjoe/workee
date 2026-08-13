#!/usr/bin/env python3
"""
Azure Virtual Desktop (AVD) Web Client automation module.

Uses Playwright to automate login to the AVD web client, handle OAuth2 PKCE
redirects, and extract VM listings via the feed discovery API.

MFA is detected but NOT handled — the script logs a clear message and exits.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from azure_wrapper.resolution import get_resolution

# ---------------------------------------------------------------------------
# Constants from the AVD web client research
# ---------------------------------------------------------------------------

AVD_ENTRY_URL = "https://windows.cloud.microsoft"
AVD_DASHBOARD_URL = "https://windows.cloud.microsoft/#/devices"
AVD_SPA_REDIRECT = "https://windows.cloud.microsoft/spa-signin-oidc"
FEED_DISCOVERY_API = "https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery"
SPA_CLIENT_ID = "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"
WVD_SCOPE = "https://www.wvd.microsoft.com/User.Access"
LOGIN_AUTHORITY = "https://login.microsoftonline.com"

# Chromium flags required for RDP WebAssembly (used for VM sessions later;
# included now for forward-compatibility with session launch features)
CHROMIUM_ARGS = [
    "--enable-features=SharedArrayBuffer",
    "--enable-features=CrossOriginOpenerPolicy",
    "--enable-features=VaapiVideoDecoder",
]

# Microsoft Edge / Windows 10 UA string (spoofs Conditional Access checks)
EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

# Default timeouts (milliseconds)
DEFAULT_TIMEOUT = 30_000
LONG_TIMEOUT = 60_000
MFA_TIMEOUT = 15_000

# ---------------------------------------------------------------------------
# Microsoft login page selectors
# ---------------------------------------------------------------------------

SELECTORS = {
    # Email entry page
    "email_input": 'input[type="email"]',
    "email_input_fallback": '#i0116',
    "next_button": 'input[type="submit"]',
    "next_button_fallback": '#idSIButton9',
    # Password entry page
    "password_input": 'input[type="password"]',
    "password_input_fallback": '#i0118',
    "signin_button": 'input[type="submit"]',
    "signin_button_fallback": '#idSIButton9',
    # Error messages (Microsoft shows various error elements)
    "email_error": '#usernameError',
    "password_error": '#passwordError',
    "auth_error_selectors": [
        '#passwordError',
        '#usernameError',
        '[class*="error"]',
        '#loginMessage',
        '#loginError',
        'div[role="alert"]',
        'text="Your account or password is incorrect"',
        'text="is incorrect"',
        'text="Try again"',
    ],
    # MFA detection (various MFA page indicators)
    "mfa_indicators": [
        'input[name="otc"]',                     # OTP code input
        '#idTxtBx_SAOTCC_OTC',                   # SMS/voice code input
        '#idDiv_SAOTCS_Title',                    # "Enter code" title
        '#idDiv_SAOTCAS_Title',                   # "Approve sign-in" title
        '#idRichContext_DisplaySign',             # Authenticator number matching
        'text="Approve sign in request"',        # Push notification prompt
        'text="Enter code"',                     # Generic code entry
        'text="Verify your identity"',            # Generic MFA
        'text="We sent a code"',                 # SMS/email code
        'text="Checking your identity"',         # MFA in progress
        'text="Don\'t have access to your authenticator"',  # MFA help link
    ],
    # Stay signed in (KMSI)
    "kmsi_yes": '#idSIButton9',
    "kmsi_no": '#idBtn_Back',
}

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read an environment variable, stripping whitespace."""
    val = os.environ.get(key, default)
    if val is not None:
        val = val.strip()
    return val


def _debug(msg: str) -> None:
    """Print a timestamped debug message to stderr."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# AVD Client
# ---------------------------------------------------------------------------

class AVDClient:
    """
    Headless browser automation client for Azure Virtual Desktop web access.

    Usage:
        client = AVDClient(email="user@contoso.com", password="...")
        client.login()
        vms = client.list_vms()
        for vm in vms:
            print(vm["name"])
        client.close()
    """

    def __init__(
        self,
        email: str,
        password: str,
        headless: bool = True,
        storage_state_path: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        resolution: str | dict | None = None,
    ):
        self.email = email
        self.password = password
        self.headless = headless
        self.storage_state_path = storage_state_path
        self.timeout = timeout
        self.resolution = resolution

        # Internal state
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._authenticated = False
        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "AVDClient":
        """Launch the Chromium browser and create a context."""
        _debug("Starting Chromium browser (headless)" if self.headless else "Starting Chromium browser (visible)")

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=CHROMIUM_ARGS,
        )

        # Resolve viewport dynamically with auto-detection support
        viewport = get_resolution(configured=self.resolution)

        context_kwargs = {
            "user_agent": EDGE_UA,
            "viewport": viewport,
            "locale": "en-US",
            "timezone_id": "America/Chicago",
        }

        # Load saved session state if available
        if self.storage_state_path and os.path.exists(self.storage_state_path):
            _debug(f"Loading saved session state from {self.storage_state_path}")
            context_kwargs["storage_state"] = self.storage_state_path

        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout)

        return self

    def close(self) -> None:
        """Clean up browser resources."""
        _debug("Closing browser")
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._authenticated = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """
        Perform full login flow: navigate → email → password → MFA detection.

        Returns True if authentication succeeded (no MFA required).
        Returns False if MFA was detected (script exits/stops).
        Raises RuntimeError on authentication failure.
        """
        _debug("Starting login flow")

        # Step 1: Navigate to AVD entry — triggers OAuth2 redirect
        _debug(f"Navigating to {AVD_ENTRY_URL}")
        self._page.goto(AVD_ENTRY_URL, wait_until="domcontentloaded")

        # Step 2: Wait for the login page to load
        self._wait_for_login_page()

        # Step 3: Enter email
        self._enter_email()

        # Step 4: Enter password (raises on auth error)
        self._enter_password()

        # Step 5: Poll for post-password state — MFA, KMSI, error, or dashboard
        outcome = self._poll_post_password_state()

        if outcome == "mfa":
            _debug("MFA page detected — exiting (MFA not handled by this script)")
            self._log_mfa_message()
            return False
        elif outcome == "error":
            # _enter_password should have already raised, but double-check
            auth_error_text = self._detect_auth_error()
            raise RuntimeError(
                f"Authentication failed: {auth_error_text or 'Unknown error'}\n"
                f"Verify AVD_EMAIL and AVD_PASSWORD are correct."
            )
        elif outcome == "kmsi":
            _debug("KMSI prompt handled")

        # Step 6: Wait for post-login redirect back to AVD
        self._wait_for_dashboard()

        # Step 7: Extract access token from localStorage
        self._extract_access_token()

        _debug("Login complete — access token obtained")
        self._authenticated = True
        return True

    def save_session(self, path: str) -> None:
        """Persist browser state (cookies + localStorage) to disk for session reuse."""
        _debug(f"Saving session state to {path}")
        self._context.storage_state(path=path)
        print(f"Session state saved to {path}")

    # ------------------------------------------------------------------
    # VM Listing
    # ------------------------------------------------------------------

    def list_vms(self) -> list[dict]:
        """
        Retrieve available VMs via the feed discovery API.

        Returns a list of dicts with keys: name, kind, workspace, resource_id.
        """
        if not self._authenticated:
            self.login()

        if not self._access_token:
            # Try extracting token from the loaded page state
            self._page.goto(AVD_DASHBOARD_URL, wait_until="domcontentloaded")
            self._page.wait_for_timeout(2000)
            self._extract_access_token()

        if not self._access_token:
            raise RuntimeError("No access token available — cannot call feed discovery API")

        _debug("Calling feed discovery API")
        response = self._page.request.get(
            FEED_DISCOVERY_API,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )

        if not response.ok:
            body = response.text()[:500]
            raise RuntimeError(
                f"Feed discovery API returned {response.status}: {body}"
            )

        return self._parse_feed_response(response.json())

    def list_vms_from_dom(self) -> list[dict]:
        """
        Fallback: scrape VM names from the dashboard DOM.

        Used when the feed discovery API is unavailable or the token
        cannot be extracted from localStorage.
        """
        if not self._authenticated:
            self.login()

        _debug("Navigating to dashboard for DOM scraping")
        self._page.goto(AVD_DASHBOARD_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(3000)

        vms = []

        # The AVD dashboard renders resources as cards/tiles. Common patterns:
        # - Elements with class containing "card", "tile", "resource"
        # - Sections with role="listitem" containing VM/desktop info
        selectors_to_try = [
            '[class*="resource-card"]',
            '[class*="ResourceCard"]',
            '[data-testid*="resource"]',
            'a[href*="/webclient/avd/"]',
            '[role="listitem"]',
        ]

        for sel in selectors_to_try:
            elements = self._page.query_selector_all(sel)
            if elements:
                for el in elements:
                    text = el.inner_text().strip()
                    if text and len(text) > 1:
                        href = el.get_attribute("href") or ""
                        vms.append({
                            "name": text.split("\n")[0],
                            "kind": "desktop",
                            "workspace": "",
                            "resource_id": self._extract_guid(href),
                        })
                if vms:
                    break

        if not vms:
            # Last resort: grab all visible text and look for VM-like patterns
            body_text = self._page.inner_text("body")
            _debug(f"Dashboard body text (first 500 chars):\n{body_text[:500]}")

        return vms

    # ------------------------------------------------------------------
    # Internal: login page helpers
    # ------------------------------------------------------------------

    def _wait_for_login_page(self) -> None:
        """Wait for the Microsoft login page to render."""
        _debug("Waiting for Microsoft login page")
        try:
            self._page.wait_for_url(
                f"{LOGIN_AUTHORITY}/**",
                timeout=LONG_TIMEOUT,
            )
            _debug(f"Redirected to: {self._page.url}")
        except PlaywrightTimeout:
            # The navigation might be client-side; check window.location
            try:
                real_url = self._page.evaluate("() => window.location.href")
            except Exception:
                real_url = self._page.url
            _debug(f"Current URL (no server-side redirect): {real_url}")
            if "login" not in real_url.lower():
                self._page.wait_for_load_state("networkidle")
                _debug(f"Page loaded at: {real_url}")

    def _enter_email(self) -> None:
        """Fill the email field and click Next."""
        _debug("Looking for email input field")

        # Try multiple selectors for robustness
        email_input = self._find_element(
            [SELECTORS["email_input"], SELECTORS["email_input_fallback"]],
            timeout=15_000,
        )

        if not email_input:
            # Some tenants show a different page (e.g., already signed in)
            _debug("No email input found — checking if already authenticated")
            if AVD_DASHBOARD_URL.split("#")[0] in self._page.url or "/devices" in self._page.url:
                _debug("Already on dashboard — skipping login")
                self._authenticated = True
                return
            raise RuntimeError(f"Could not find email input field on page: {self._page.url}")

        email_input.click()
        email_input.fill(self.email)
        _debug(f"Entered email: {self.email}")

        next_btn = self._find_element(
            [SELECTORS["next_button"], SELECTORS["next_button_fallback"]],
            timeout=5_000,
        )
        if next_btn:
            next_btn.click()
            _debug("Clicked Next")

        # Microsoft stays on same URL after email entry — toggles view via JS.
        # Wait for either the password field to appear, an error, or MFA.
        _debug("Waiting for password field (or error/MFA) after email...")
        try:
            self._page.wait_for_selector(
                f"{SELECTORS['password_input']}, "
                f"{SELECTORS['password_input_fallback']}, "
                f"{SELECTORS['email_error']}, "
                "input[name='otc'], "
                "#idDiv_SAOTCS_Title",
                timeout=15_000,
                state="attached",
            )
        except PlaywrightTimeout:
            pass

        self._page.wait_for_timeout(500)

        # Check for email error
        error = self._page.query_selector(SELECTORS["email_error"])
        if error and error.is_visible():
            err_text = error.inner_text().strip()
            raise RuntimeError(f"Email validation error: {err_text}")

        # Check if MFA appeared directly (some tenants skip password for MFA-first flows)
        if self._detect_mfa():
            return

    def _enter_password(self) -> None:
        """Fill the password field and click Sign in."""
        _debug("Looking for password input field")

        password_input = self._find_element(
            [SELECTORS["password_input"], SELECTORS["password_input_fallback"]],
            timeout=15_000,
        )

        if not password_input:
            # Check for MFA page (some tenants go straight to MFA after email)
            if self._detect_mfa():
                return
            # Check if we're already past login
            if AVD_DASHBOARD_URL.split("#")[0] in self._page.url or "/devices" in self._page.url:
                _debug("Already on dashboard — skipping password")
                self._authenticated = True
                return
            raise RuntimeError(f"Could not find password input field on page: {self._page.url}")

        password_input.click()
        password_input.fill(self.password)
        _debug("Entered password")

        signin_btn = self._find_element(
            [SELECTORS["signin_button"], SELECTORS["signin_button_fallback"]],
            timeout=5_000,
        )
        if signin_btn:
            signin_btn.click()
            _debug("Clicked Sign in")

        # Wait for the password field to disappear (form submission processed)
        # OR for an error/MFA/dashboard to appear. Microsoft stays on the same URL.
        _debug("Waiting for response after password submission...")
        try:
            self._page.wait_for_selector(
                f"{SELECTORS['password_input']}",
                timeout=10_000,
                state="detached",
            )
            _debug("Password form dismissed — checking next state")
        except PlaywrightTimeout:
            # Password field still visible — likely an auth error on the same page
            _debug("Password field still visible — possible auth error")
            pass

        self._page.wait_for_timeout(1000)

        # Check for auth errors FIRST (before MFA/KMSI detection)
        auth_error_text = self._detect_auth_error()
        if auth_error_text:
            raise RuntimeError(
                f"Authentication failed: {auth_error_text}\n"
                f"Verify AVD_EMAIL and AVD_PASSWORD are correct."
            )

    def _detect_mfa(self) -> bool:
        """
        Check if the current page is an MFA challenge page.

        Returns True if MFA is detected.
        """
        # Give the page a moment to settle
        self._page.wait_for_timeout(500)

        # Check real browser URL (window.location) — more reliable than Playwright's
        # tracked URL for SPA-style transitions
        try:
            real_url = self._page.evaluate("() => window.location.href").lower()
        except Exception:
            real_url = self._page.url.lower()

        # URL-based detection
        mfa_url_patterns = [
            "/oauth2/v2.0/mfa",
            "mfachallenge",
            "convergedmfa",
            "mfarequired",
            "authenticator",
            "/mfa",
        ]
        for pattern in mfa_url_patterns:
            if pattern in real_url:
                _debug(f"MFA detected via URL pattern: {pattern}")
                return True

        # DOM-based detection
        for indicator in SELECTORS["mfa_indicators"]:
            try:
                if indicator.startswith('text="'):
                    text_match = indicator[6:-1]
                    locator = self._page.get_by_text(text_match, exact=False)
                    if locator.count() > 0 and locator.first.is_visible():
                        _debug(f"MFA detected via text: {text_match}")
                        return True
                else:
                    el = self._page.query_selector(indicator)
                    if el and el.is_visible():
                        _debug(f"MFA detected via selector: {indicator}")
                        return True
            except Exception:
                continue

        return False

    def _detect_auth_error(self) -> Optional[str]:
        """
        Check if the current page shows an authentication error.

        Returns the error message text if found, None otherwise.
        """
        for selector in SELECTORS["auth_error_selectors"]:
            try:
                if selector.startswith('text="'):
                    text_match = selector[6:-1]
                    locator = self._page.get_by_text(text_match, exact=False)
                    if locator.count() > 0:
                        el = locator.first
                        if el.is_visible():
                            return el.inner_text().strip()[:200]
                else:
                    el = self._page.query_selector(selector)
                    if el and el.is_visible():
                        text = el.inner_text().strip()
                        if text and len(text) > 1:
                            return text[:200]
            except Exception:
                continue

        return None

    def _poll_post_password_state(self, poll_timeout: int = 30_000) -> str:
        """
        After password submission, poll for the next page state.

        Microsoft login can transition to several states after password entry:
        - MFA challenge page (input for code, "Approve sign in", etc.)
        - "Stay signed in?" (KMSI) prompt
        - Error message ("Your account or password is incorrect")
        - OAuth2 redirect back to AVD (success)

        Returns one of: "mfa", "kmsi", "error", "dashboard", "unknown"
        """
        _debug("Polling for post-password state...")
        deadline = time.monotonic() + (poll_timeout / 1000)

        while time.monotonic() < deadline:
            # Check for MFA first (most likely next state in enterprise tenants)
            if self._detect_mfa():
                return "mfa"

            # Check for KMSI prompt
            kmsi_btn = self._page.query_selector(SELECTORS["kmsi_yes"])
            if kmsi_btn and kmsi_btn.is_visible():
                _debug("KMSI prompt detected — clicking 'Yes'")
                kmsi_btn.click()
                self._page.wait_for_timeout(1000)
                return "kmsi"

            # Check for auth errors
            error_text = self._detect_auth_error()
            if error_text:
                return "error"

            # Check if we've arrived at the dashboard (success)
            try:
                real_url = self._page.evaluate("() => window.location.href")
            except Exception:
                real_url = self._page.url

            if ("windows.cloud.microsoft" in real_url and
                ("/devices" in real_url or "/spa-signin-oidc" in real_url)):
                _debug("Post-login redirect detected — reached AVD")
                return "dashboard"

            # Check if we're no longer on the login page at all
            if LOGIN_AUTHORITY not in real_url:
                _debug(f"Left Microsoft login — now at: {real_url[:100]}")
                return "dashboard"

            self._page.wait_for_timeout(500)

        _debug("Poll timeout — no clear post-password state detected")
        return "unknown"

    def _log_mfa_message(self) -> None:
        """Print a clear MFA-detected message for the user."""
        print(
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  MFA (Multi-Factor Authentication) DETECTED             ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            "║  This script does not handle MFA. To proceed:           ║\n"
            "║                                                        ║\n"
            "║  Option 1 — Session reuse (recommended):                ║\n"
            "║    Set AVD_HEADLESS=false, run interactively to        ║\n"
            "║    complete MFA, then the session is saved to           ║\n"
            "║    $AVD_STORAGE_STATE for future automated runs.        ║\n"
            "║                                                        ║\n"
            "║  Option 2 — TOTP integration (Phase 2):                 ║\n"
            "║    Set AVD_TOTP_SECRET to your TOTP secret key         ║\n"
            "║    (not yet implemented in this version).               ║\n"
            "╚══════════════════════════════════════════════════════════╝\n"
        )

    def _handle_kmsi(self) -> None:
        """Handle the 'Stay signed in?' prompt if it appears."""
        _debug("Checking for 'Stay signed in?' prompt")
        try:
            yes_btn = self._page.wait_for_selector(
                SELECTORS["kmsi_yes"],
                timeout=5_000,
            )
            if yes_btn and yes_btn.is_visible():
                _debug("Clicking 'Yes' on KMSI prompt")
                yes_btn.click()
                self._page.wait_for_load_state("networkidle")
        except PlaywrightTimeout:
            pass  # KMSI prompt didn't appear — normal

    def _wait_for_dashboard(self) -> None:
        """Wait for the post-login redirect to the AVD dashboard."""
        _debug("Waiting for AVD dashboard to load")

        try:
            # The SPA redirects through: /spa-signin-oidc#code=... → /#/devices
            # We wait for the dashboard URL pattern
            self._page.wait_for_url(
                "**/windows.cloud.microsoft/**/devices**",
                timeout=LONG_TIMEOUT,
            )
            _debug(f"Dashboard loaded at: {self._page.url}")
        except PlaywrightTimeout:
            # We might be at /spa-signin-oidc still processing
            _debug(f"Timeout waiting for dashboard. Current URL: {self._page.url}")
            # Give MSAL.js time to process the token exchange
            self._page.wait_for_timeout(5000)
            _debug(f"URL after wait: {self._page.url}")

        # Ensure the SPA has finished rendering
        self._page.wait_for_load_state("networkidle")
        self._page.wait_for_timeout(2000)

    # ------------------------------------------------------------------
    # Internal: token extraction
    # ------------------------------------------------------------------

    def _extract_access_token(self) -> None:
        """Extract the WVD access token from localStorage (MSAL cache)."""
        _debug("Extracting access token from localStorage")

        token = self._page.evaluate(
            """
            () => {
                // MSAL stores tokens with keys matching the pattern:
                // {homeAccountId}-login.windows.net-accesstoken-{clientId}-{tenant}-{scopes}
                const keys = Object.keys(localStorage);
                const clientId = '%s';
                const scopeTag = 'accesstoken';

                for (const key of keys) {
                    if (key.includes(clientId) && key.includes(scopeTag)) {
                        try {
                            const entry = JSON.parse(localStorage.getItem(key));
                            if (entry && entry.secret) {
                                return entry.secret;
                            }
                        } catch (e) {
                            continue;
                        }
                    }
                }
                return null;
            }
            """
            % SPA_CLIENT_ID
        )

        if token:
            self._access_token = token
            _debug("Access token extracted successfully")
        else:
            _debug("Could not extract access token from localStorage")

    # ------------------------------------------------------------------
    # Internal: feed parsing
    # ------------------------------------------------------------------

    def _parse_feed_response(self, data: dict) -> list[dict]:
        """
        Parse the feed discovery API response into a list of resources.

        The response contains workspace information, desktop/application
        names, icons, and RDP connection configuration.
        """
        vms = []

        # The feed discovery response structure (based on AVD API):
        # {
        #   "value": [
        #     {
        #       "workspace": { "name": "...", "id": "..." },
        #       "resources": [
        #         { "name": "...", "resourceType": "Desktop|RemoteApp",
        #           "icon": "...", "resourceId": "..." }
        #       ]
        #     }
        #   ]
        # }
        resources_list = data.get("value", []) if isinstance(data, dict) else data

        for workspace_entry in resources_list:
            workspace = workspace_entry.get("workspace", {})
            workspace_name = workspace.get("name", workspace.get("friendlyName", "Unknown"))

            for resource in workspace_entry.get("resources", []):
                vm = {
                    "name": resource.get("name", resource.get("friendlyName", "Unnamed")),
                    "kind": resource.get("resourceType", resource.get("type", "unknown")),
                    "workspace": workspace_name,
                    "resource_id": resource.get("resourceId", resource.get("id", "")),
                    "icon": resource.get("icon", ""),
                }
                vms.append(vm)

        # If the response doesn't match the expected structure, try common alternatives
        if not vms:
            # Flattened response: array of resource objects directly
            if isinstance(data, list):
                for item in data:
                    vms.append({
                        "name": item.get("name", item.get("friendlyName", "Unnamed")),
                        "kind": item.get("resourceType", item.get("type", "unknown")),
                        "workspace": item.get("workspaceName", ""),
                        "resource_id": item.get("resourceId", item.get("id", "")),
                    })

        return vms

    # ------------------------------------------------------------------
    # Internal: utilities
    # ------------------------------------------------------------------

    def _find_element(self, selectors: list[str], timeout: int = 10_000) -> Optional:
        """Try multiple selectors, return the first visible element found."""
        deadline = time.monotonic() + (timeout / 1000)
        while time.monotonic() < deadline:
            for sel in selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        return el
                except Exception:
                    continue
            self._page.wait_for_timeout(200)
        return None

    @staticmethod
    def _extract_guid(text: str) -> str:
        """Extract a GUID from a string (e.g., URL path segment)."""
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        )
        return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# Main entry point (when run directly)
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point: login and list VMs."""
    email = _env("AVD_EMAIL")
    password = _env("AVD_PASSWORD")
    storage_state = _env("AVD_STORAGE_STATE", os.path.expanduser("~/.avd_session.json"))
    headless = _env("AVD_HEADLESS", "true").lower() not in ("false", "0", "no")
    resolution = _env("AZURE_RESOLUTION")

    if not email or not password:
        print(
            "ERROR: AVD_EMAIL and AVD_PASSWORD environment variables are required.\n"
            "Usage:\n"
            "  export AVD_EMAIL='user@contoso.com'\n"
            "  export AVD_PASSWORD='your-password'\n"
            "  python3 avd_client.py\n"
            "\n"
            "Optional:\n"
            "  AVD_STORAGE_STATE  Path for session persistence (default: ~/.avd_session.json)\n"
            "  AVD_HEADLESS       true/false (default: true)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    client = AVDClient(
        email=email,
        password=password,
        headless=headless,
        storage_state_path=storage_state,
        resolution=resolution,
    )

    try:
        client.start()

        # Attempt login
        authenticated = client.login()

        if not authenticated:
            # MFA detected — save partial state and exit
            client.save_session(storage_state)
            sys.exit(2)

        # Save session for future reuse
        client.save_session(storage_state)

        # Retrieve VM list
        print("\n=== Azure Virtual Desktop — Available VMs ===\n")
        try:
            vms = client.list_vms()
        except Exception as e:
            _debug(f"Feed discovery API failed: {e}")
            _debug("Falling back to DOM scraping")
            vms = client.list_vms_from_dom()

        if not vms:
            print("No VMs found. The account may have no provisioned resources,")
            print("or the feed format has changed. Try DOM scraping:")
            vms = client.list_vms_from_dom()

        for i, vm in enumerate(vms, 1):
            print(f"  [{i}] {vm['name']}")
            print(f"      Kind:      {vm['kind']}")
            print(f"      Workspace: {vm['workspace']}")
            if vm.get("resource_id"):
                print(f"      Resource:  {vm['resource_id']}")
            print()

        if vms:
            print(f"Total: {len(vms)} resource(s)")
        else:
            print("No resources found.")

    finally:
        client.close()


if __name__ == "__main__":
    main()
