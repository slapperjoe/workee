"""MfaManager — MFA detection and handling with signal/wait pattern.

Extends the battle-tested MFA logic from avd_mfa.py with:
- asyncio.Event for caller coordination (signal/wait)
- Auto-detection of 4 MFA prompt types
- TOTP auto-generation, push notification polling, manual code entry
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from azure_wrapper.qr_auth import QrAuthConfig, QrAuthManager, QrAuthServer

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class MfaPromptType(Enum):
    """Recognised MFA prompt types."""
    CODE_INPUT = "code_input"
    PUSH_PENDING = "push_pending"
    DEVICE_SELECT = "device_select"
    VERIFY_IDENTITY = "verify_identity"
    QR_CODE = "qr_code"


class MfaStatus(Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    NO_MFA_PROMPT = "no_mfa_prompt"
    UNSUPPORTED_PROMPT = "unsupported_prompt"
    ERROR = "error"
    SKIPPED = "skipped"
    WAITING = "waiting"  # waiting for caller to provide code
    QR_PENDING = "qr_pending"  # waiting for QR code scan


@dataclass
class MfaResult:
    success: bool
    status: MfaStatus
    reason: str = ""
    method_used: str | None = None
    prompt_type: str | None = None
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Selectors (battle-tested from avd_mfa.py)
# ---------------------------------------------------------------------------

MFA_CODE_INPUT_SELECTORS: list[tuple[str, str]] = [
    ('input[name="otc"]', "otc_input"),
    ("#idTxtBx_SAOTCC_OTC", "saotcc_input"),
    ('input[id*="otc"]', "otc_input_any"),
]

MFA_PUSH_SELECTORS: list[tuple[str, str]] = [
    ("#idDiv_SAOTCAS_Title", "push_approval_title"),
    ("#idRichContext_DisplaySign", "push_notification_sent"),
]

MFA_DEVICE_SELECTORS: list[tuple[str, str]] = [
    ('input[value="PhoneAppNotification"]', "phone_app_notification"),
    ('input[value="OneWaySMS"]', "sms_option"),
    ('input[value="PhoneAppOTP"]', "totp_app_option"),
]

MFA_VERIFY_IDENTITY_SELECTORS: list[tuple[str, str]] = [
    ("#idDiv_SAASDS_Title", "verify_identity_title"),
    ("#loginHeader", "login_header"),
]

# QR code selectors — Microsoft Authenticator QR enrolment / government QR flow
MFA_QR_SELECTORS: list[tuple[str, str]] = [
    ("#idDiv_SAOTCRQ_Title", "qr_code_title"),
    ("canvas.qr-code", "qr_canvas"),
    ("img.qr-code", "qr_image"),
    ("[data-testid='qr-code']", "qr_testid"),
    ("#idTxtBx_SAOTCC_OTC", "saotcc_input"),
]


# ---------------------------------------------------------------------------
# MfaManager
# ---------------------------------------------------------------------------

class MfaManager:
    """Detect and handle Microsoft Entra ID MFA prompts.

    Supports:
      - TOTP code generation (via pyotp)
      - Push notification polling
      - Manual code entry via stdin or MFA hook
      - Signal/wait pattern: set mfa_event when caller input is needed
    """

    def __init__(
        self,
        method: str = "auto",
        totp_secret: str | None = None,
        timeout: float = 120.0,
        post_submit_timeout: float = 60.0,
        mfa_hook=None,
        qr_manager: QrAuthManager | None = None,
        qr_config: QrAuthConfig | None = None,
    ):
        self.method = method
        self.totp_secret = totp_secret
        self.timeout = timeout
        self.post_submit_timeout = post_submit_timeout
        self.mfa_hook = mfa_hook  # Optional async callable(code) -> str

        # QR code auth (Australian government compliance)
        self.qr_manager = qr_manager
        self.qr_config = qr_config
        self._qr_server: QrAuthServer | None = None
        self._qr_login_url: str = ""
        self._qr_task: asyncio.Task | None = None

        # Signal/wait pattern
        self.mfa_event = asyncio.Event()
        self.mfa_pending = False
        self._pending_code: str | None = None

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    async def detect(self, page: Page, timeout_ms: int = 5000) -> MfaPromptType | None:
        """Detect what kind of MFA prompt is currently visible.

        Returns MfaPromptType enum or None.
        """
        detector_groups: list[tuple[list[tuple[str, str]], MfaPromptType]] = [
            (MFA_CODE_INPUT_SELECTORS, MfaPromptType.CODE_INPUT),
            (MFA_PUSH_SELECTORS, MfaPromptType.PUSH_PENDING),
            (MFA_DEVICE_SELECTORS, MfaPromptType.DEVICE_SELECT),
            # QR_CODE checked BEFORE VERIFY_IDENTITY — the '#loginHeader'
            # selector in VERIFY_IDENTITY is too generic and would match
            # QR pages before the QR-specific selectors can fire.
            (MFA_QR_SELECTORS, MfaPromptType.QR_CODE),
            (MFA_VERIFY_IDENTITY_SELECTORS, MfaPromptType.VERIFY_IDENTITY),
        ]

        for selector_group, prompt_type in detector_groups:
            for sel, _name in selector_group:
                try:
                    if await page.locator(sel).is_visible(timeout=timeout_ms):
                        logger.debug(
                            "MFA prompt detected: %s (selector: %s)",
                            prompt_type.value,
                            sel,
                        )
                        return prompt_type
                except Exception:
                    continue

        return None

    # ------------------------------------------------------------------
    # Main handler
    # ------------------------------------------------------------------

    async def handle(self, page: Page) -> MfaResult:
        """Detect and handle an MFA prompt on the current page.

        This is the main entry point. Call after entering password and
        clicking "Sign in". Returns MfaResult.

        On prompts that can't be auto-resolved, sets mfa_event and
        returns MfaResult(success=False, status=WAITING).
        """
        start = time.monotonic()
        self.mfa_pending = False
        self.mfa_event.clear()

        # Wait for an MFA prompt to appear
        deadline = start + self.timeout
        prompt_type: MfaPromptType | None = None

        while time.monotonic() < deadline:
            prompt_type = await self.detect(page, timeout_ms=3000)
            if prompt_type is not None:
                break
            # Also check: did we leave the login page without prompting?
            if "login.microsoftonline.com" not in (page.url or ""):
                logger.info("Left login page — no MFA needed")
                return MfaResult(
                    success=True,
                    status=MfaStatus.SKIPPED,
                    reason="No MFA prompt detected; already past login",
                    method_used="skip",
                    elapsed_seconds=time.monotonic() - start,
                )
            await asyncio.sleep(1)

        if prompt_type is None:
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.TIMEOUT,
                reason="MFA prompt did not appear within timeout",
                elapsed_seconds=elapsed,
            )

        # Dispatch to handler
        logger.info("MFA prompt type: %s", prompt_type.value)

        if prompt_type == MfaPromptType.CODE_INPUT:
            return await self._handle_code_input(page, start)
        elif prompt_type == MfaPromptType.PUSH_PENDING:
            return await self._handle_push_pending(page, start)
        elif prompt_type == MfaPromptType.DEVICE_SELECT:
            return await self._handle_device_selection(page, start)
        elif prompt_type == MfaPromptType.QR_CODE:
            return await self._handle_qr_code(page, start)
        elif prompt_type == MfaPromptType.VERIFY_IDENTITY:
            # Generic prompt — check more specifically
            return await self._handle_verify_identity(page, start)

        elapsed = time.monotonic() - start
        return MfaResult(
            success=False,
            status=MfaStatus.UNSUPPORTED_PROMPT,
            reason=f"Unknown prompt type: {prompt_type}",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Public: caller can provide code / approval
    # ------------------------------------------------------------------

    def provide_code(self, code: str) -> None:
        """Store a user-provided MFA code for the handler to submit."""
        self._pending_code = code
        self.mfa_event.set()

    def provide_approval(self) -> None:
        """Signal that the user approved a push notification."""
        self.mfa_event.set()

    # ------------------------------------------------------------------
    # Internal: code input
    # ------------------------------------------------------------------

    async def _handle_code_input(self, page: Page, start: float) -> MfaResult:
        """Handle a code input MFA prompt."""
        if self.method == "totp" and self.totp_secret:
            # Auto-generate TOTP code
            return await self._handle_totp(page, start)

        elif self.method == "manual":
            # Prompt on stdin
            return await self._handle_manual_stdin(page, start)

        elif self.method == "auto":
            if self.totp_secret:
                result = await self._handle_totp(page, start)
                if result.success:
                    return result
                logger.info("TOTP failed, falling back to manual")
            return await self._handle_manual(page, start)

        else:
            return await self._handle_manual(page, start)

    # ------------------------------------------------------------------
    # TOTP
    # ------------------------------------------------------------------

    async def _handle_totp(self, page: Page, start: float) -> MfaResult:
        """Generate and submit a TOTP code."""
        try:
            import pyotp
        except ImportError:
            logger.warning("pyotp not installed — cannot auto-generate TOTP")
            return MfaResult(
                success=False,
                status=MfaStatus.ERROR,
                reason="pyotp not installed; install with: pip install pyotp",
                method_used="totp",
            )

        try:
            totp = pyotp.TOTP(self.totp_secret)  # type: ignore[arg-type]
            code = totp.now()
        except Exception as exc:
            return MfaResult(
                success=False,
                status=MfaStatus.ERROR,
                reason=f"TOTP code generation failed: {exc}",
                method_used="totp",
            )

        logger.info("Generated TOTP code")
        submitted = await self._submit_code(page, code)
        elapsed = time.monotonic() - start
        if submitted:
            return MfaResult(
                success=True,
                status=MfaStatus.SUCCESS,
                reason="TOTP code submitted",
                method_used="totp",
                prompt_type="code_input",
                elapsed_seconds=elapsed,
            )
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason="Could not locate code input field for TOTP submission",
            method_used="totp",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Manual code entry
    # ------------------------------------------------------------------

    async def _handle_manual_stdin(self, page: Page, start: float) -> MfaResult:
        """Prompt for code on stdin."""
        loop = asyncio.get_event_loop()

        async def _read_line() -> str:
            return await loop.run_in_executor(None, sys.stdin.readline)

        remaining = max(0, (start + self.timeout) - time.monotonic())
        print(
            f"\n[MFA] Multi-factor authentication required. "
            f"Enter the code from your authenticator app "
            f"(timeout in {remaining:.0f}s):",
            flush=True,
        )
        try:
            code = await asyncio.wait_for(
                _read_line(), timeout=remaining
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.TIMEOUT,
                reason="Operator did not enter MFA code in time",
                method_used="manual",
                elapsed_seconds=elapsed,
            )

        code = code.strip()
        if not code:
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.ERROR,
                reason="Empty MFA code",
                method_used="manual",
                elapsed_seconds=elapsed,
            )

        return await self._submit_and_report(page, code, "manual", start)

    async def _handle_manual(self, page: Page, start: float) -> MfaResult:
        """Prompt for code via hook or signal/wait pattern."""
        if self.mfa_hook:
            # Caller provided a hook — use it
            try:
                code = await self.mfa_hook("Enter MFA code: ")
            except Exception as exc:
                return MfaResult(
                    success=False,
                    status=MfaStatus.ERROR,
                    reason=f"MFA hook failed: {exc}",
                    method_used="manual",
                )
            if not code or not code.strip():
                return MfaResult(
                    success=False,
                    status=MfaStatus.ERROR,
                    reason="MFA hook returned empty code",
                    method_used="manual",
                )
            return await self._submit_and_report(page, code.strip(), "manual", start)

        # Signal/wait: tell the caller we need input
        self.mfa_pending = True
        self.mfa_event.set()
        logger.info("MFA code needed — waiting for caller to provide_code()")

        # Wait for caller to call provide_code()
        remaining = max(0, (start + self.timeout) - time.monotonic())
        try:
            await asyncio.wait_for(self.mfa_event.wait(), timeout=remaining)
            # Event was set again by provide_code()
            # Re-clear so we don't trigger again
            self.mfa_event.clear()
            self.mfa_pending = False
        except asyncio.TimeoutError:
            self.mfa_pending = False
            self.mfa_event.clear()
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.TIMEOUT,
                reason="Caller did not provide MFA code in time",
                method_used="manual",
                elapsed_seconds=elapsed,
            )

        code = self._pending_code
        self._pending_code = None
        if not code:
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.ERROR,
                reason="No MFA code provided by caller",
                method_used="manual",
                elapsed_seconds=elapsed,
            )

        return await self._submit_and_report(page, code, "manual", start)

    # ------------------------------------------------------------------
    # Push notification
    # ------------------------------------------------------------------

    async def _handle_push_pending(self, page: Page, start: float) -> MfaResult:
        """Poll until push notification is approved or timeout."""
        logger.info("Push notification pending — waiting for approval on device")
        deadline = start + self.timeout

        while time.monotonic() < deadline:
            # Check if push screen is gone (user approved)
            still_showing = False
            for sel, _name in MFA_PUSH_SELECTORS:
                try:
                    if await page.locator(sel).is_visible(timeout=2000):
                        still_showing = True
                        break
                except Exception:
                    continue

            if not still_showing:
                elapsed = time.monotonic() - start
                logger.info("Push notification resolved (user approved)")
                return MfaResult(
                    success=True,
                    status=MfaStatus.SUCCESS,
                    reason="Push notification approved on device",
                    method_used="push",
                    prompt_type="push_pending",
                    elapsed_seconds=elapsed,
                )
            await asyncio.sleep(2)

        elapsed = time.monotonic() - start
        return MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="Push notification was not approved within timeout",
            method_used="push",
            prompt_type="push_pending",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Device selection
    # ------------------------------------------------------------------

    async def _handle_device_selection(self, page: Page, start: float) -> MfaResult:
        """Try to select a code-based MFA method from the device selection page."""
        logger.info("Device selection page — attempting to pick code method")

        # Try "Use verification code instead" link
        try:
            code_link = page.locator("#idAADTOTP_Description")
            if await code_link.is_visible(timeout=2000):
                await code_link.click()
                logger.info("Clicked 'Use verification code instead'")
                return MfaResult(
                    success=True,
                    status=MfaStatus.SUCCESS,
                    reason="Selected verification code method",
                    method_used="code_link",
                    prompt_type="device_select",
                    elapsed_seconds=time.monotonic() - start,
                )
        except Exception:
            pass

        # Try to select PhoneAppOTP
        try:
            totp_radio = page.locator('input[value="PhoneAppOTP"]')
            if await totp_radio.is_visible(timeout=2000):
                await totp_radio.check()
                for btn_sel in [
                    "#idSIButton9",
                    'input[type="submit"]',
                    'button[type="submit"]',
                ]:
                    try:
                        btn = page.locator(btn_sel)
                        if await btn.is_visible(timeout=2000):
                            await btn.click()
                            break
                    except Exception:
                        continue
                logger.info("Selected PhoneAppOTP method")
                return MfaResult(
                    success=True,
                    status=MfaStatus.SUCCESS,
                    reason="Selected TOTP app method",
                    method_used="device_totp",
                    prompt_type="device_select",
                    elapsed_seconds=time.monotonic() - start,
                )
        except Exception:
            pass

        elapsed = time.monotonic() - start
        return MfaResult(
            success=False,
            status=MfaStatus.UNSUPPORTED_PROMPT,
            reason="Could not auto-select code-based MFA method on device selection page",
            prompt_type="device_select",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Verify identity (generic)
    # ------------------------------------------------------------------

    async def _handle_verify_identity(self, page: Page, start: float) -> MfaResult:
        """Handle a generic 'verify your identity' prompt by re-detecting more specifically.

        The '#loginHeader' selector that triggers this is very generic and
        can match on pages that are actually showing a QR code, code input,
        push notification, or device selection. We re-detect and dispatch
        to the correct handler.
        """
        logger.debug("Generic verify identity prompt — probing for specific type")
        # Try re-detecting more specifically — check ALL known prompt types
        # (not just CODE_INPUT and PUSH_PENDING, which was the old behaviour
        #  that starved QR_CODE and DEVICE_SELECT detection).
        for _ in range(3):
            prompt_type = await self.detect(page, timeout_ms=2000)
            if prompt_type == MfaPromptType.CODE_INPUT:
                return await self._handle_code_input(page, start)
            if prompt_type == MfaPromptType.PUSH_PENDING:
                return await self._handle_push_pending(page, start)
            if prompt_type == MfaPromptType.QR_CODE:
                return await self._handle_qr_code(page, start)
            if prompt_type == MfaPromptType.DEVICE_SELECT:
                return await self._handle_device_selection(page, start)
            await asyncio.sleep(1)

        elapsed = time.monotonic() - start
        return MfaResult(
            success=False,
            status=MfaStatus.UNSUPPORTED_PROMPT,
            reason="Generic verify identity prompt — could not determine specific MFA type",
            prompt_type="verify_identity",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # QR code authentication (Australian government compliance)
    # ------------------------------------------------------------------

    async def _handle_qr_code(self, page: Page, start: float) -> MfaResult:
        """Handle a QR code MFA prompt.

        When mfa_method='qr', starts a local QR auth server that displays
        a QR code for scanning by a government-approved app. The caller
        can access the login URL via qr_login_url property.

        When mfa_method is not 'qr', falls through to manual handling.
        """
        if self.method == "qr" and self.qr_manager is not None:
            return await self._handle_qr_flow(start)
        elif self.method == "auto":
            # If QR manager is available, use it
            if self.qr_manager is not None:
                return await self._handle_qr_flow(start)
            # Otherwise treat as unsupported — caller can retry with manual
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.QR_PENDING,
                reason=(
                    "QR code prompt detected but no QR auth manager configured. "
                    "Set mfa_method='qr' and provide qr_signing_key."
                ),
                prompt_type="qr_code",
                elapsed_seconds=elapsed,
            )
        else:
            elapsed = time.monotonic() - start
            return MfaResult(
                success=False,
                status=MfaStatus.UNSUPPORTED_PROMPT,
                reason=f"QR code prompt not handled by mfa_method={self.method!r}",
                prompt_type="qr_code",
                elapsed_seconds=elapsed,
            )

    async def _handle_qr_flow(self, start: float) -> MfaResult:
        """Run the QR code authentication flow.

        Starts a local server with a QR code, signals the caller
        via mfa_pending/mfa_event, and waits for the government app
        to scan and submit a validated token.
        """
        challenge = self.qr_manager.generate_challenge()  # type: ignore[union-attr]
        self._qr_server = QrAuthServer(self.qr_manager, self.qr_config)
        self._qr_login_url = await self._qr_server.start(challenge)

        logger.info("QR login page: %s", self._qr_login_url)

        # Signal the caller that we need QR scan
        self.mfa_pending = True
        self.mfa_event.set()

        # Wait for the token to arrive
        remaining = max(0, (start + self.timeout) - time.monotonic())
        try:
            self._qr_task = asyncio.create_task(
                self._qr_server.wait_for_token(timeout=remaining)
            )
            token = await self._qr_task
        except asyncio.CancelledError:
            token = None
        except Exception:
            token = None

        elapsed = time.monotonic() - start

        # Cleanup
        if self._qr_server:
            await self._qr_server.stop()
            self._qr_server = None

        if token:
            logger.info("QR code scanned and token validated")
            return MfaResult(
                success=True,
                status=MfaStatus.SUCCESS,
                reason="QR code scanned and token validated by government app",
                method_used="qr_code",
                prompt_type="qr_code",
                elapsed_seconds=elapsed,
            )

        self.mfa_pending = False
        return MfaResult(
            success=False,
            status=MfaStatus.QR_PENDING,
            reason="Timed out waiting for QR code scan",
            method_used="qr_code",
            prompt_type="qr_code",
            elapsed_seconds=elapsed,
        )

    @property
    def qr_login_url(self) -> str:
        """URL of the QR code login page, if server is running."""
        return self._qr_login_url

    async def cancel_qr(self) -> None:
        """Cancel the QR auth flow and stop the server."""
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        if self._qr_server:
            await self._qr_server.stop()
            self._qr_server = None
        self._qr_login_url = ""
        self.mfa_pending = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _submit_code(self, page: Page, code: str) -> bool:
        """Fill MFA code input and submit."""
        filled = False
        for sel, name in MFA_CODE_INPUT_SELECTORS:
            try:
                field = page.locator(sel)
                if await field.is_visible(timeout=2000):
                    await field.fill(code)
                    logger.debug("Filled MFA code into %s (%s)", name, sel)
                    filled = True
                    break
            except Exception:
                continue

        if not filled:
            logger.warning("Could not locate MFA code input field")
            return False

        # Submit
        submit_selectors = [
            'input[type="submit"]',
            'button[type="submit"]',
            "#idSIButton9",
            "#idSubmit_SAOTCC_Continue",
            "button:has-text('Verify')",
            "button:has-text('Continue')",
        ]
        for sel in submit_selectors:
            try:
                btn = page.locator(sel)
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.debug("Clicked submit: %s", sel)
                    return True
            except Exception:
                continue

        # Last resort: press Enter
        logger.debug("No submit button found; pressing Enter")
        await page.keyboard.press("Enter")
        return True

    async def _submit_and_report(
        self, page: Page, code: str, method: str, start: float
    ) -> MfaResult:
        """Submit a code and return a MfaResult."""
        submitted = await self._submit_code(page, code)
        elapsed = time.monotonic() - start
        if submitted:
            return MfaResult(
                success=True,
                status=MfaStatus.SUCCESS,
                reason=f"Manual MFA code submitted",
                method_used=method,
                prompt_type="code_input",
                elapsed_seconds=elapsed,
            )
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason="Could not locate code input field for submission",
            method_used=method,
            elapsed_seconds=elapsed,
        )
