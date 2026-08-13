"""AuthManager — Microsoft Entra ID login flow for Azure sessions.

Handles the full OAuth2 PKCE login flow through a Playwright browser page.
Extracted and refined from avd_login.py with support for:
- Session reuse via persistent context or storageState
- MFA coordination via MfaManager (signal/wait)
- KMSI prompt dismissal
- Token extraction from localStorage
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING

from azure_wrapper.config import AzureConfig

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

LOGIN_HOST = "login.microsoftonline.com"

# ---------------------------------------------------------------------------
# Selectors for post-email prompt detection
# ---------------------------------------------------------------------------

# Password field selectors — direct match on Microsoft Entra ID password page
_PASSWORD_SELECTORS: list[str] = [
    'input[type="password"]',
    'input[name="passwd"]',
]

# MFA / QR code selectors that may appear instead of password after email entry.
# These indicate an MFA-before-password tenant configuration.
_POST_EMAIL_MFA_SELECTORS: list[str] = [
    # Code input (TOTP / SMS / phone call)
    'input[name="otc"]',
    "#idTxtBx_SAOTCC_OTC",
    # Push notification approval
    "#idDiv_SAOTCAS_Title",
    "#idRichContext_DisplaySign",
    # Device selection (phone app, SMS, etc.)
    'input[value="PhoneAppNotification"]',
    'input[value="OneWaySMS"]',
    'input[value="PhoneAppOTP"]',
    # QR code enrolment / Microsoft Authenticator setup
    "#idDiv_SAOTCRQ_Title",
    "canvas.qr-code",
    # Generic verify identity
    "#idDiv_SAASDS_Title",
]

# Entra may show an MFA method picker before the password field.  The
# password-first dashboard flow must select the password option explicitly.
_PASSWORD_SWITCH_SELECTORS: list[str] = [
    "#idA_PWD_SwitchToPassword",
    "#idA_PWD_SwitchToPasswordLink",
    'a[data-bind*="switchToPassword"]',
    'button[data-bind*="switchToPassword"]',
]


class AuthPhase(Enum):
    """State returned by authenticate() to tell the caller what to do next.

    DONE               — Fully authenticated; session is ready to use.
    PASSWORD_ENTERED   — Email + password submitted; caller should handle MFA
                         (including QR code) now. Password was entered first.
    NEEDS_PASSWORD     — Email done, password field is visible and ready.
                         Caller may optionally handle pre-password MFA first,
                         then call enter_password().
    MFA_BEFORE_PASSWORD — Email done, but an MFA / QR prompt appeared BEFORE
                          the password field. Caller MUST resolve MFA first,
                          then call enter_password(). This handles Azure
                          tenants configured for MFA-before-password ordering.
    FAILED             — Authentication could not proceed.
    """

    DONE = "done"
    PASSWORD_ENTERED = "password_entered"
    NEEDS_PASSWORD = "needs_password"
    MFA_BEFORE_PASSWORD = "mfa_before_password"
    FAILED = "failed"


class AuthManager:
    """Manage browser auth lifecycle: login, session reuse, re-auth.

    Supports two authentication orderings:

    1. Password-first (default Microsoft Entra ID):
       email → password → MFA/QR

    2. MFA-first (tenant-configured):
       email → MFA/QR → password

    The authenticate() method returns an AuthPhase so the caller
    (AzureSession.start()) can orchestrate the correct sequence.
    """

    def __init__(self, config: AzureConfig):
        self.config = config
        self._access_token: str | None = None
        # Track whether email has already been entered in this auth attempt,
        # so the caller can continue from the right point after resolving MFA.
        self._email_done: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def authenticate(
        self,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        reuse_state: bool = True,
    ) -> AuthPhase:
        """Begin login — navigate, enter email, detect next step.

        Returns an AuthPhase telling the caller what to do next:

        - DONE: Already authenticated (session reuse or direct access).
        - PASSWORD_ENTERED: Email + password submitted; caller should
          now handle MFA (including QR code).
        - NEEDS_PASSWORD: Email done; password field is visible.
          Caller should call enter_password() next.
        - MFA_BEFORE_PASSWORD: Email done but an MFA/QR prompt appeared
          before password. Caller MUST resolve MFA first via
          MfaManager.handle(), then call enter_password().
        - FAILED: Could not reach the login page or email entry failed.

        Args:
            browser: Playwright Browser instance.
            context: Playwright BrowserContext (may be reused on failure).
            page: Playwright Page to use for the login flow.
            reuse_state: If True, try to detect if already authenticated.

        Returns:
            AuthPhase indicating the next step.
        """
        entry_url = self._get_entry_url()

        # --- Step 1: Navigate to entry point ---
        logger.info("Navigating to %s", entry_url)
        await page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # --- Step 2: Check if already authenticated ---
        if reuse_state and await self._is_authenticated(page):
            logger.info("Already authenticated (reused session)")
            await self._save_state(context)
            return AuthPhase.DONE

        # --- Step 3: If not already on login page, wait for redirect ---
        if LOGIN_HOST not in page.url:
            logger.info("Waiting for redirect to Microsoft login...")
            try:
                await page.wait_for_url(f"**/{LOGIN_HOST}/**", timeout=30000)
            except Exception:
                if await self._is_authenticated(page):
                    logger.info("Reached AVD without explicit login redirect")
                    await self._save_state(context)
                    return AuthPhase.DONE
                logger.warning(
                    "Did not redirect to Microsoft login; URL: %s", page.url
                )

        # --- Step 4: Enter email ---
        logger.info("Entering email...")
        await self._fill_email(page)
        if await self._is_authenticated(page):
            await self._save_state(context)
            return AuthPhase.DONE

        self._email_done = True

        # --- Step 5: Detect what Microsoft shows after email ---
        # Two possible paths (tenant-dependent):
        #   a) Password-first: password field is shown next. Enter it now,
        #      then return PASSWORD_ENTERED so the caller handles MFA.
        #   b) MFA-first: an MFA prompt or QR code is shown instead.
        #      Return MFA_BEFORE_PASSWORD so the caller handles MFA first,
        #      then calls enter_password().

        logger.info("Detecting post-email prompt type...")
        prompt_type = await self._detect_post_email_prompt(page, timeout_ms=5000)

        if prompt_type == "password":
            # --- Path (a): Password is next ---
            # Enter password immediately; caller handles MFA after.
            logger.info("Password field detected — entering password")
            entered = await self._fill_password(page)
            if not entered:
                logger.error("Password field found but could not fill it")
                return AuthPhase.FAILED
            await asyncio.sleep(2)
            return AuthPhase.PASSWORD_ENTERED

        elif prompt_type == "mfa":
            # --- Path (b): method picker appeared before password ---
            # Do not start MFA here: the dashboard contract is password first.
            logger.info("MFA/QR prompt appeared before password — switching to password")
            if await self._switch_to_password(page):
                entered = await self._fill_password(page)
                if entered:
                    await asyncio.sleep(2)
                    return AuthPhase.PASSWORD_ENTERED
            logger.error("Could not switch from MFA/QR prompt to password")
            return AuthPhase.FAILED

        elif prompt_type == "authenticated":
            # Redirected past login after email (e.g., federated auth)
            logger.info("Already past login after email entry")
            await self._save_state(context)
            return AuthPhase.DONE

        else:
            # Neither password nor MFA visible — may be transient
            logger.warning(
                "Could not detect password field or MFA prompt after email. "
                "URL: %s",
                page.url,
            )
            return AuthPhase.FAILED

    async def enter_password(self, page: Page) -> AuthPhase:
        """Enter password on the current page and submit.

        Call this after resolving any pre-password MFA. The page should
        now be showing the password field on login.microsoftonline.com.

        Returns:
            PASSWORD_ENTERED if successful, FAILED otherwise.
        """
        logger.info("Entering password (post-MFA or direct)...")
        entered = await self._fill_password(page)
        if not entered:
            logger.error("Could not fill password field")
            return AuthPhase.FAILED
        await asyncio.sleep(2)
        self._email_done = False  # Reset for next auth cycle
        return AuthPhase.PASSWORD_ENTERED

    async def reauthenticate(
        self, context: BrowserContext, page: Page
    ) -> AuthPhase:
        """Re-authenticate after session expiration.

        Uses the existing browser context — partial cookies may avoid
        a full login. Falls back to full auth if needed.
        """
        logger.info("Re-authenticating...")
        entry_url = self._get_entry_url()
        await page.goto(entry_url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        if await self._is_authenticated(page):
            logger.info("Quick re-auth succeeded — back on dashboard")
            await self._save_state(context)
            return AuthPhase.DONE

        # Fall back to full authentication via the two-phase flow.
        # authenticate() handles email → detect path → return phase.
        return await self.authenticate(
            await self._get_browser(context),
            context,
            page,
            reuse_state=False,
        )

    async def extract_access_token(self, page: Page, client_id: str) -> str | None:
        """Extract an access token from MSAL.js localStorage cache.

        Args:
            page: Playwright Page on an authenticated Azure page.
            client_id: The SPA client ID whose token to extract.

        Returns:
            Bearer token string or None.
        """
        token_js = f"""
            (() => {{
                const keys = Object.keys(localStorage);
                const clientId = '{client_id}';
                const scopeTag = 'accesstoken';
                for (const key of keys) {{
                    if (key.includes(clientId) && key.includes(scopeTag)) {{
                        try {{
                            const entry = JSON.parse(localStorage.getItem(key));
                            if (entry && entry.secret) return entry.secret;
                        }} catch (e) {{ continue; }}
                    }}
                }}
                return null;
            }})()
        """
        try:
            token = await page.evaluate(token_js)
            if token:
                self._access_token = token
                logger.debug("Access token extracted")
                return token
        except Exception as exc:
            logger.debug("Token extraction failed: %s", exc)
        return None

    @property
    def access_token(self) -> str | None:
        return self._access_token

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _detect_post_email_prompt(
        self, page: Page, timeout_ms: int = 5000
    ) -> str:
        """Detect what page state appears after email entry.

        Scans the page for known selectors to determine whether Microsoft
        is asking for a password, showing an MFA prompt, or has already
        redirected past login (e.g., federated authentication).

        Returns one of:
            "password"      — Password input field is visible.
            "mfa"           — An MFA prompt (code input, push, QR, device
                              selection, verify identity) is visible.
            "authenticated" — No longer on login.microsoftonline.com.
            "unknown"       — Could not determine the page state.

        Detection order matters: checked in priority so that MFA/QR
        prompts are recognised before more generic page elements.
        """
        # --- Check if already past login ---
        if not await self._is_authenticated(page):
            # Still on login page — try authenticated check anyway in case
            # the URL changed to a federated IdP or bypass page.
            try:
                if LOGIN_HOST not in page.url:
                    return "authenticated"
            except Exception:
                pass

        # --- Check for MFA / QR prompt FIRST (higher priority) ---
        # On MFA-first tenants, the MFA prompt appears before password.
        for sel in _POST_EMAIL_MFA_SELECTORS:
            try:
                if await page.locator(sel).is_visible(timeout=timeout_ms):
                    logger.debug(
                        "Post-email MFA/QR selector matched: %s", sel
                    )
                    return "mfa"
            except Exception:
                continue

        # --- Check for password field ---
        for sel in _PASSWORD_SELECTORS:
            try:
                if await page.locator(sel).is_visible(timeout=timeout_ms):
                    logger.debug(
                        "Post-email password selector matched: %s", sel
                    )
                    return "password"
            except Exception:
                continue

        # --- Last resort: check if page URL indicates we're past login ---
        try:
            if LOGIN_HOST not in page.url:
                return "authenticated"
        except Exception:
            pass

        return "unknown"

    async def _switch_to_password(self, page: Page) -> bool:
        """Select Entra's password option when a method picker is shown."""
        for selector in _PASSWORD_SWITCH_SELECTORS:
            try:
                control = page.locator(selector).first
                if await control.is_visible(timeout=1500):
                    await control.click()
                    return True
            except Exception:
                continue

        # IDs vary between Entra login versions; constrain fallback to the
        # exact user-facing action rather than a generic authentication link.
        for text in ("Use your password instead", "Use password instead"):
            try:
                control = page.get_by_text(text, exact=True).first
                if await control.is_visible(timeout=1500):
                    await control.click()
                    return True
            except Exception:
                continue
        return False

    async def _fill_email(self, page: Page) -> None:
        """Fill email field and click Next."""
        try:
            await page.wait_for_selector(
                'input[type="email"], input[name="loginfmt"]',
                timeout=15000,
            )
        except Exception:
            if await self._is_authenticated(page):
                return
            raise

        email_field = page.locator(
            'input[type="email"], input[name="loginfmt"]'
        ).first
        if await email_field.is_visible():
            await email_field.fill("")
            await email_field.type(self.config.email, delay=50)
        else:
            await page.fill('input[name="loginfmt"]', self.config.email)

        await page.click('input[type="submit"]')

    async def _fill_password(self, page: Page) -> bool:
        """Fill password field and click Sign in.

        Waits for the password field to appear, fills it, and submits.
        Returns True if the password was filled and submitted successfully.

        Unlike the previous version, this method no longer raises on
        timeout — it returns False so the caller can decide how to
        recover (e.g., handling MFA first on MFA-before-password tenants).
        """
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            for selector in _PASSWORD_SELECTORS:
                try:
                    field = page.locator(selector).first
                    if await field.is_visible(timeout=500):
                        await field.fill(self.config.password)
                        await page.click('input[type="submit"]')
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.25)

        logger.warning("Password field did not become visible within timeout")
        return False

    async def _is_authenticated(self, page: Page) -> bool:
        """Check if we appear authenticated (not on login page)."""
        try:
            url = page.url
            if LOGIN_HOST in url:
                return False
            if (
                "windows.cloud.microsoft" in url
                or "portal.azure.com" in url
            ):
                return True
            return LOGIN_HOST not in url
        except Exception:
            return False

    async def _save_state(self, context: BrowserContext) -> None:
        """Save browser storage state to disk."""
        if self.config.persistence_mode == "storage_state":
            try:
                await context.storage_state(
                    path=str(self.config.storage_state_path)
                )
                logger.debug(
                    "Saved auth state to %s",
                    self.config.storage_state_path,
                )
            except Exception as exc:
                logger.warning("Could not save auth state: %s", exc)

    def _get_entry_url(self) -> str:
        """Return the entry URL for the configured backend."""
        if self.config.backend == "portal":
            return "https://portal.azure.com/"
        return "https://windows.microsoft.cloud"

    @staticmethod
    async def _get_browser(context: BrowserContext):
        """Get the browser from a context — Playwright internal."""
        return context.browser
