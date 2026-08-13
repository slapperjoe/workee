#!/usr/bin/env python3
"""
avd_login.py — Core login script for Azure Virtual Desktop web client.

Handles the full authentication flow including email, password, MFA, and
session persistence. Uses avd_mfa.py for MFA handling.

Usage:
    # Set credentials via environment or .env file (see config.example.env)
    python avd_login.py

    # To reuse an existing session:
    python avd_login.py --state auth-state.json

    # To force re-authentication:
    python avd_login.py --no-reuse

Environment variables (or .env file):
    AVD_EMAIL, AVD_PASSWORD       — credentials
    AVD_MFA_METHOD                — "totp", "manual", or "auto"
    AVD_TOTP_SECRET               — TOTP secret (for totp mode)
    AVD_MFA_TIMEOUT               — MFA prompt timeout seconds (default 120)
    AVD_MFA_POST_SUBMIT_TIMEOUT   — post-MFA-submit timeout seconds (default 60)
    AVD_STORAGE_STATE_PATH        — where to save/load auth state
    AVD_HEADED                    — "true" to show browser window
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from avd_mfa import MfaResult, MfaStatus, handle_mfa
from azure_wrapper.resolution import get_resolution, PRESETS

# Load .env from script directory
_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()  # fallback: cwd

logger = logging.getLogger("avd_login")

# ---------------------------------------------------------------------------
# URL constants (from research doc)
# ---------------------------------------------------------------------------

ENTRY_URL = "https://windows.microsoft.cloud"
DASHBOARD_URL = "https://windows.cloud.microsoft/#/devices"
SPA_REDIRECT_PATH = "/spa-signin-oidc"
LOGIN_HOST = "login.microsoftonline.com"

# MSAL.js SPA client ID for AVD (public)
CLIENT_ID = "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"

# Edge/Windows User-Agent for Conditional Access compliance
EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

# Chromium flags required for AVD RDP WebAssembly (from research doc)
CHROMIUM_ARGS = [
    "--enable-features=SharedArrayBuffer",
    "--enable-features=CrossOriginOpenerPolicy",
    "--enable-features=VaapiVideoDecoder",
]


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

def _get_config() -> dict:
    """Read configuration from environment with sensible defaults."""
    email = os.getenv("AVD_EMAIL")
    password = os.getenv("AVD_PASSWORD")

    if not email or not password:
        logger.error(
            "AVD_EMAIL and AVD_PASSWORD must be set in environment or .env file. "
            "Copy config.example.env to .env and fill in your credentials."
        )
        sys.exit(1)

    return {
        "email": email,
        "password": password,
        "mfa_method": os.getenv("AVD_MFA_METHOD", "auto"),
        "totp_secret": os.getenv("AVD_TOTP_SECRET") or None,
        "mfa_timeout": float(os.getenv("AVD_MFA_TIMEOUT", "120")),
        "mfa_post_submit_timeout": float(os.getenv("AVD_MFA_POST_SUBMIT_TIMEOUT", "60")),
        "storage_state_path": os.getenv("AVD_STORAGE_STATE_PATH", "./auth-state.json"),
        "headed": os.getenv("AVD_HEADED", "").lower() in ("1", "true", "yes"),
    }


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

async def _wait_for_dashboard(page, timeout: float = 30.0) -> bool:
    """Wait for the AVD dashboard to load after successful auth."""
    import time as _time
    start = _time.monotonic()
    deadline = start + timeout

    while _time.monotonic() < deadline:
        url = page.url
        if "windows.cloud.microsoft" in url and "login.microsoftonline.com" not in url:
            # We're back on the AVD domain — wait for dashboard content
            logger.info("Redirected to AVD domain: %s", url[:100])
            try:
                # Wait for a dashboard element
                await page.wait_for_load_state("networkidle", timeout=15000)
                return True
            except Exception:
                pass
            return True  # at least we're on the right domain

        await asyncio.sleep(1)

    logger.warning("Dashboard did not load within %.0fs", timeout)
    return False


async def _wait_for_redirect_away_from_login(page, timeout: float = 300.0) -> bool:
    """Wait until the page navigates away from login.microsoftonline.com.

    Returns True if we left the login page, False on timeout.
    """
    import time as _time
    start = _time.monotonic()
    deadline = start + timeout

    while _time.monotonic() < deadline:
        url = page.url
        if LOGIN_HOST not in url:
            logger.info("Left login page → %s", url[:100])
            return True
        await asyncio.sleep(1)

    logger.warning("Still on login page after %.0fs", timeout)
    return False


async def authenticate(
    *,
    email: str,
    password: str,
    mfa_method: str = "auto",
    totp_secret: str | None = None,
    mfa_timeout: float = 120.0,
    mfa_post_submit_timeout: float = 60.0,
    storage_state_path: str | None = None,
    headed: bool = False,
    reuse_state: bool = True,
    resolution: str | dict | None = None,
) -> tuple[object, object, bool]:
    """Perform full Azure Virtual Desktop authentication using Playwright.

    This is the main entry point for programmatic use. It:
      1. Optionally loads a saved auth state (session reuse)
      2. If that fails or is disabled, performs full login flow
      3. Handles MFA via avd_mfa.handle_mfa()
      4. Saves the new auth state for future reuse
      5. Returns (browser, context, success)

    Args:
        email: Azure AD / Microsoft account email.
        password: Account password.
        mfa_method: "totp", "manual", or "auto".
        totp_secret: TOTP secret for automated MFA.
        mfa_timeout: Seconds to wait for MFA prompt.
        mfa_post_submit_timeout: Seconds to wait after MFA submit.
        storage_state_path: Path to save/load Playwright storage state.
        headed: If True, show browser window.
        reuse_state: If True, try to load saved auth state first.

    Returns:
        Tuple of (browser, context, success).
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    logger.info("Playwright started")

    browser = await pw.chromium.launch(
        headless=not headed,
        args=CHROMIUM_ARGS,
    )

    # Resolve viewport dynamically with auto-detection support
    viewport = get_resolution(configured=resolution)

    context = None
    authenticated = False

    # --- Attempt session reuse ---
    state_file = Path(storage_state_path) if storage_state_path else None
    if reuse_state and state_file and state_file.exists():
        logger.info("Loading saved auth state from %s", state_file)
        try:
            context = await browser.new_context(
                storage_state=str(state_file),
                user_agent=EDGE_UA,
                viewport=viewport,
            )
            page = await context.new_page()
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)

            # Give the SPA a moment to use refresh tokens
            await asyncio.sleep(5)
            current_url = page.url

            if "login.microsoftonline.com" not in current_url and "windows.cloud.microsoft" in current_url:
                logger.info("Session reuse successful — already authenticated")
                authenticated = True
                return browser, context, True
            else:
                logger.info("Saved session expired or invalid; performing fresh login")
                await context.close()
                context = None
        except Exception as exc:
            logger.warning("Session reuse failed: %s", exc)
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
                context = None

    # --- Fresh login flow ---
    if context is None:
        context = await browser.new_context(
            user_agent=EDGE_UA,
            viewport=viewport,
            # Don't save state until we're authenticated
        )

    page = await context.new_page()

    try:
        # Step 1: Navigate to entry point
        logger.info("Navigating to %s ...", ENTRY_URL)
        await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        current_url = page.url
        logger.info("Current URL after entry: %s", current_url[:150])

        # If we're already on the dashboard, we might have been redirected
        # via an existing session cookie. That's fine — we're authenticated.
        if LOGIN_HOST not in current_url and "windows.cloud.microsoft" in current_url:
            logger.info("Already authenticated (no login redirect)")
            authenticated = True
            if state_file:
                await context.storage_state(path=str(state_file))
                logger.info("Saved auth state to %s", state_file)
            return browser, context, True

        # Step 2: Wait for the Microsoft login page
        if LOGIN_HOST not in current_url:
            logger.info("Waiting for redirect to Microsoft login...")
            try:
                await page.wait_for_url(f"**/{LOGIN_HOST}/**", timeout=30000)
            except Exception:
                logger.warning("Did not redirect to Microsoft login; current URL: %s", page.url)

        # Step 3: Enter email
        logger.info("Entering email...")
        await page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=15000)
        email_field = page.locator('input[type="email"], input[name="loginfmt"]').first
        if await email_field.is_visible():
            # Clear any pre-filled value
            await email_field.fill("")
            await email_field.type(email, delay=50)
        else:
            # Fallback: try the classic Microsoft login field
            await page.fill('input[name="loginfmt"]', email)

        # Click "Next"
        await page.click('input[type="submit"]')

        # Step 4: Wait for password field
        logger.info("Waiting for password field...")
        await page.wait_for_selector(
            'input[type="password"], input[name="passwd"]',
            timeout=30000,
        )
        await asyncio.sleep(1)

        # Microsoft sometimes shows "work/school account" vs "personal account"
        # after email entry. If we see the password field, proceed.
        pw_field = page.locator('input[type="password"], input[name="passwd"]').first
        if await pw_field.is_visible():
            await pw_field.fill(password)
        else:
            logger.error("Password field not visible after email entry")
            return browser, context, False

        # Click "Sign in"
        await page.click('input[type="submit"]')

        # Step 5: Handle MFA
        logger.info("Waiting for potential MFA prompt...")
        await asyncio.sleep(2)

        # Check if we need MFA
        mfa_result: MfaResult = await handle_mfa(
            page,
            method=mfa_method,
            totp_secret=totp_secret,
            timeout=mfa_timeout,
            post_submit_timeout=mfa_post_submit_timeout,
        )

        if not mfa_result.success:
            logger.error(
                "MFA handling failed: [%s] %s (elapsed: %.1fs, method: %s)",
                mfa_result.status.value,
                mfa_result.reason,
                mfa_result.elapsed_seconds,
                mfa_result.method_used or "none",
            )
            # Even if MFA failed, the user might have resolved it manually
            # (e.g., push notification). Let's check where we are.
            current_url = page.url
            if LOGIN_HOST in current_url:
                logger.error("Still on login page after MFA failure — authentication failed")
                return browser, context, False
            logger.info("Despite MFA handler reporting failure, we left the login page — proceeding")
        else:
            logger.info(
                "MFA result: %s — %s (%.1fs)",
                mfa_result.status.value,
                mfa_result.reason,
                mfa_result.elapsed_seconds,
            )

        # Step 6: Wait for redirect to AVD
        logger.info("Waiting for redirect back to AVD dashboard...")
        redirected = await _wait_for_redirect_away_from_login(page, timeout=60.0)
        if not redirected:
            logger.warning("Did not leave login page after MFA; checking for 'Stay signed in' prompt...")
            # Check for "Stay signed in?" (KMSI) prompt
            try:
                kmsi_yes = page.locator("#idSIButton9")
                if await kmsi_yes.is_visible(timeout=3000):
                    await kmsi_yes.click()
                    logger.info("Clicked 'Yes' on 'Stay signed in?' prompt")
                    redirected = await _wait_for_redirect_away_from_login(page, timeout=30.0)
            except Exception:
                pass

        if not redirected:
            logger.error("Authentication did not complete — still on login page")
            return browser, context, False

        # Step 7: Wait for dashboard
        dashboard_loaded = await _wait_for_dashboard(page, timeout=30.0)
        if dashboard_loaded:
            logger.info("Dashboard loaded successfully")
            authenticated = True

            # Save auth state for future reuse
            if state_file:
                await context.storage_state(path=str(state_file))
                logger.info("Saved auth state to %s", state_file)

    except Exception as exc:
        logger.exception("Authentication error: %s", exc)
        return browser, context, False

    return browser, context, authenticated


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Azure Virtual Desktop login automation",
    )
    parser.add_argument(
        "--state", "-s",
        default=os.getenv("AVD_STORAGE_STATE_PATH", "./auth-state.json"),
        help="Path to save/load auth state JSON (default: ./auth-state.json)",
    )
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="Force fresh login even if saved state exists",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=os.getenv("AVD_HEADED", "").lower() in ("1", "true", "yes"),
        help="Show browser window",
    )
    # Build resolution help from PRESETS for single-source-of-truth
    _res_help_parts = []
    for name in ("desktop", "laptop", "hd", "fhd", "qhd", "4k"):
        if name in PRESETS:
            p = PRESETS[name]
            _res_help_parts.append(f"'{name}' ({p['width']}x{p['height']})")
    _res_help = (
        "Browser viewport: " + ", ".join(_res_help_parts)
        + ", 'WxH' like '1920x1080', or 'auto'"
    )
    parser.add_argument(
        "--resolution", "-r",
        default=os.getenv("AZURE_RESOLUTION"),
        help=_res_help,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = _get_config()

    logger.info("AVD Login — starting authentication")
    logger.info("  Email: %s", config["email"])
    logger.info("  MFA method: %s", config["mfa_method"])
    logger.info("  State file: %s", args.state)
    logger.info("  Session reuse: %s", not args.no_reuse)

    browser, context, success = await authenticate(
        email=config["email"],
        password=config["password"],
        mfa_method=config["mfa_method"],
        totp_secret=config["totp_secret"],
        mfa_timeout=config["mfa_timeout"],
        mfa_post_submit_timeout=config["mfa_post_submit_timeout"],
        storage_state_path=args.state,
        headed=args.headed,
        reuse_state=not args.no_reuse,
        resolution=args.resolution,
    )

    if success:
        logger.info("Authentication successful!")
        # At this point, context is authenticated.
        # The caller can use context to navigate to VMs, call APIs, etc.
        #
        # For now, verify VM listing works:
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            logger.info("Dashboard URL: %s", page.url)
            # Try to load feed discovery API via page evaluation
            feed_data = await page.evaluate("""
                async () => {
                    const resp = await fetch(
                        'https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery',
                        { credentials: 'include' }
                    );
                    if (!resp.ok) return { error: resp.status, statusText: resp.statusText };
                    return await resp.text();
                }
            """)
            if isinstance(feed_data, dict) and "error" in feed_data:
                logger.warning("Feed discovery API returned error: %s", feed_data)
            else:
                logger.info("Feed discovery API responded (%d chars)", len(str(feed_data)))
        except Exception as exc:
            logger.warning("Could not verify VM listing: %s", exc)

        # Don't close the browser — let caller or user manage it
        logger.info("Browser is running. Use context for subsequent operations.")
        logger.info("Press Ctrl+C to exit and close browser.")

        # Keep alive until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
    else:
        logger.error("Authentication failed!")
        sys.exit(1)

    await context.close()
    await browser.close()


if __name__ == "__main__":
    asyncio.run(_main())
