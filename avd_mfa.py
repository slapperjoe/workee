"""
avd_mfa.py — MFA detection and handling for Azure Virtual Desktop login.

Supports two methods:
  - TOTP: automated code generation via pyotp (requires TOTP secret)
  - Manual: prompts the operator on stdin for the code

Detects MFA prompts on the Microsoft sign-in page and submits the
appropriate response. Designed to be called from avd_login.py or any
Playwright-based automation script.

Usage:
    from avd_mfa import handle_mfa, MfaResult

    result = await handle_mfa(page, method="auto", totp_secret="...", timeout=120)
    if result.success:
        print("MFA completed")
    else:
        print(f"MFA failed: {result.reason}")
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class MfaMethod(Enum):
    TOTP = "totp"
    MANUAL = "manual"
    AUTO = "auto"


class MfaStatus(Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    NO_MFA_PROMPT = "no_mfa_prompt"
    UNSUPPORTED_PROMPT = "unsupported_prompt"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class MfaResult:
    success: bool
    status: MfaStatus
    reason: str = ""
    method_used: str | None = None

    # Additional context for logging
    prompt_type: str | None = None
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# MFA prompt detection selectors
# ---------------------------------------------------------------------------

# Ordered from most-specific to least-specific to avoid false matches.
# Each tuple: (selector, prompt_type_label)
MFA_CODE_INPUT_SELECTORS: list[tuple[str, str]] = [
    # Classic Microsoft "Enter code" textbox
    ('input[name="otc"]', "otc_input"),
    # Newer Microsoft "Enter code" textbox (Office 365 / Entra ID style)
    ("#idTxtBx_SAOTCC_OTC", "saotcc_input"),
    # Generic OTP code input
    ('input[id*="otc"]', "otc_input_any"),
]

MFA_PUSH_SELECTORS: list[tuple[str, str]] = [
    # "Approve sign in request" heading (push notification sent)
    ("#idDiv_SAOTCAS_Title", "push_approval_title"),
    # "We've sent a notification to your mobile device"
    ("#idRichContext_DisplaySign", "push_notification_sent"),
]

MFA_DEVICE_SELECTION_SELECTORS: list[tuple[str, str]] = [
    # SMS option radio
    ('input[value="PhoneAppNotification"]', "phone_app_notification"),
    ('input[value="OneWaySMS"]', "sms_option"),
    ('input[value="PhoneAppOTP"]', "totp_app_option"),
]

MFA_ALTERNATIVE_METHODS_SELECTOR = "#idAADTOTP_Description"

# General "choose verification method" heading
MFA_VERIFY_IDENTITY_SELECTORS: list[tuple[str, str]] = [
    ("#idDiv_SAASDS_Title", "verify_identity_title"),
    ("#loginHeader", "login_header"),
]

# "Use verification code" link — when MFA push is shown but user wants TOTP
MFA_USE_CODE_LINK_SELECTOR = "#idAADTOTP_Description"


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

async def detect_mfa_prompt(
    page: "Page",
    timeout_ms: int = 5000,
) -> str | None:
    """Detect what kind of MFA prompt is currently visible on the page.

    Returns one of:
      - "code_input"     — a code entry field is visible
      - "push_pending"   — push notification is pending
      - "device_select"  — device/method selection page
      - "verify_identity" — generic verification prompt
      - None             — no recognizable MFA prompt found
    """
    selectors_to_try: list[tuple[list[tuple[str, str]], str]] = [
        (MFA_CODE_INPUT_SELECTORS, "code_input"),
        (MFA_PUSH_SELECTORS, "push_pending"),
        (MFA_DEVICE_SELECTION_SELECTORS, "device_select"),
        (MFA_VERIFY_IDENTITY_SELECTORS, "verify_identity"),
    ]

    for selector_group, label in selectors_to_try:
        for sel, _name in selector_group:
            try:
                if await page.locator(sel).is_visible(timeout=timeout_ms):
                    logger.debug("MFA prompt detected: %s (selector: %s)", label, sel)
                    return label
            except Exception:
                continue

    return None


# ---------------------------------------------------------------------------
# TOTP code generation
# ---------------------------------------------------------------------------

async def _submit_code(page: "Page", code: str) -> bool:
    """Fill the MFA code input field and submit the form.

    Returns True if the field was found and filled, False otherwise.
    """
    filled = False
    for sel, name in MFA_CODE_INPUT_SELECTORS:
        try:
            field = page.locator(sel)
            if await field.is_visible(timeout=2000):
                await field.fill(code)
                logger.info("Filled MFA code into %s (%s)", name, sel)
                filled = True
                break
        except Exception:
            continue

    if not filled:
        logger.warning("Could not locate MFA code input field")
        return False

    # Submit — try common submit buttons in order
    submit_selectors = [
        'input[type="submit"]',
        'button[type="submit"]',
        "#idSIButton9",  # "Verify" button on Microsoft login
        "#idSubmit_SAOTCC_Continue",
        "button:has-text('Verify')",
        "button:has-text('Continue')",
    ]
    for sel in submit_selectors:
        try:
            btn = page.locator(sel)
            if await btn.is_visible(timeout=2000):
                await btn.click()
                logger.info("Clicked submit button: %s", sel)
                return True
        except Exception:
            continue

    # Last resort: press Enter in the code field
    logger.info("No submit button found; pressing Enter")
    await page.keyboard.press("Enter")
    return True


async def _handle_totp(
    page: "Page",
    totp_secret: str,
    timeout: float,
) -> MfaResult:
    """Handle MFA using TOTP code generation."""
    import pyotp

    start = asyncio.get_event_loop().time()
    deadline = start + timeout

    # Wait for the code input field to appear
    field_found = False
    while asyncio.get_event_loop().time() < deadline:
        for sel, _name in MFA_CODE_INPUT_SELECTORS:
            try:
                if await page.locator(sel).is_visible(timeout=2000):
                    field_found = True
                    break
            except Exception:
                continue
        if field_found:
            break
        await asyncio.sleep(1)

    if not field_found:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="MFA code input field did not appear within timeout",
            method_used="totp",
            elapsed_seconds=elapsed,
        )

    try:
        code = pyotp.TOTP(totp_secret).now()
    except Exception as exc:
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason=f"TOTP code generation failed: {exc}",
            method_used="totp",
        )

    logger.info("Generated TOTP code (expires in ~%ds)", pyotp.TOTP(totp_secret).interval - (int(asyncio.get_event_loop().time()) % pyotp.TOTP(totp_secret).interval))

    submitted = await _submit_code(page, code)
    if not submitted:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason="Could not locate code input field to submit TOTP code",
            method_used="totp",
            elapsed_seconds=elapsed,
        )

    elapsed = asyncio.get_event_loop().time() - start
    return MfaResult(
        success=True,
        status=MfaStatus.SUCCESS,
        reason="TOTP code submitted",
        method_used="totp",
        prompt_type="code_input",
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Manual code entry
# ---------------------------------------------------------------------------

async def _handle_manual(
    page: "Page",
    timeout: float,
) -> MfaResult:
    """Handle MFA by prompting the operator for a code on stdin."""

    async def _read_stdin() -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, sys.stdin.readline)

    start = asyncio.get_event_loop().time()
    deadline = start + timeout

    # Wait for the code input field to appear
    field_found = False
    while asyncio.get_event_loop().time() < deadline:
        for sel, _name in MFA_CODE_INPUT_SELECTORS:
            try:
                if await page.locator(sel).is_visible(timeout=2000):
                    field_found = True
                    break
            except Exception:
                continue
        if field_found:
            break
        await asyncio.sleep(1)

    if not field_found:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="MFA code input field did not appear within timeout",
            method_used="manual",
            elapsed_seconds=elapsed,
        )

    # Prompt the operator
    remaining = max(0, deadline - asyncio.get_event_loop().time())
    print(
        f"\n[MFA] Multi-factor authentication required. "
        f"Enter the code from your authenticator app (timeout in {remaining:.0f}s):",
        flush=True,
    )

    try:
        code = await asyncio.wait_for(_read_stdin(), timeout=remaining)
    except asyncio.TimeoutError:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="Operator did not enter MFA code in time",
            method_used="manual",
            elapsed_seconds=elapsed,
        )

    code = code.strip()
    if not code:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason="Empty MFA code entered",
            method_used="manual",
            elapsed_seconds=elapsed,
        )

    submitted = await _submit_code(page, code)
    if not submitted:
        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.ERROR,
            reason="Could not locate code input field to submit manual code",
            method_used="manual",
            elapsed_seconds=elapsed,
        )

    elapsed = asyncio.get_event_loop().time() - start
    return MfaResult(
        success=True,
        status=MfaStatus.SUCCESS,
        reason="Manual MFA code submitted",
        method_used="manual",
        prompt_type="code_input",
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Push notification handling
# ---------------------------------------------------------------------------

async def _handle_push_pending(
    page: "Page",
    timeout: float,
) -> MfaResult:
    """Handle MFA push notification — wait for user to approve on phone.

    Returns SUCCESS once the push notification screen disappears
    (user approved), or TIMEOUT if it doesn't resolve.
    """
    start = asyncio.get_event_loop().time()
    deadline = start + timeout

    logger.info("MFA push notification detected — waiting for approval on device")

    # Check if there's a "use verification code instead" link and click it
    # if the operator prefers code entry (only when method is "manual")
    # For now, just wait for the push to resolve.

    while asyncio.get_event_loop().time() < deadline:
        # If the push screen is gone, the user approved
        still_showing = False
        for sel, _name in MFA_PUSH_SELECTORS:
            try:
                if await page.locator(sel).is_visible(timeout=2000):
                    still_showing = True
                    break
            except Exception:
                continue

        if not still_showing:
            elapsed = asyncio.get_event_loop().time() - start
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

    elapsed = asyncio.get_event_loop().time() - start
    return MfaResult(
        success=False,
        status=MfaStatus.TIMEOUT,
        reason="Push notification was not approved within timeout",
        method_used="push",
        prompt_type="push_pending",
        elapsed_seconds=elapsed,
    )


# ---------------------------------------------------------------------------
# Device/method selection
# ---------------------------------------------------------------------------

async def _handle_device_selection(page: "Page") -> MfaResult:
    """If a device selection page appears, try to pick the TOTP/code option.

    This is best-effort — if the page has multiple MFA methods listed,
    we try to select the code-based one.
    """
    logger.info("MFA device selection page detected — attempting to select code method")

    # Try to click "Use verification code" link if present
    try:
        code_link = page.locator("#idAADTOTP_Description")
        if await code_link.is_visible(timeout=2000):
            await code_link.click()
            logger.info("Clicked 'Use verification code instead' link")
            return MfaResult(
                success=True,
                status=MfaStatus.SUCCESS,
                reason="Selected verification code method",
                method_used="code_link",
                prompt_type="device_select",
            )
    except Exception:
        pass

    # Try to select PhoneAppOTP (TOTP from authenticator app)
    try:
        totp_radio = page.locator('input[value="PhoneAppOTP"]')
        if await totp_radio.is_visible(timeout=2000):
            await totp_radio.check()
            # Click continue/verify
            for btn_sel in ["#idSIButton9", 'input[type="submit"]', 'button[type="submit"]']:
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
            )
    except Exception:
        pass

    return MfaResult(
        success=False,
        status=MfaStatus.UNSUPPORTED_PROMPT,
        reason="Could not automatically select a code-based MFA method on device selection page",
        prompt_type="device_select",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def handle_mfa(
    page: "Page",
    *,
    method: str = "auto",
    totp_secret: str | None = None,
    timeout: float = 120.0,
    post_submit_timeout: float = 60.0,
) -> MfaResult:
    """Detect and handle an MFA prompt on the Microsoft sign-in page.

    This is the main entry point. Call it after entering the password and
    clicking "Sign in". It waits for an MFA prompt, then handles it
    according to the chosen method.

    Args:
        page: Playwright Page object currently on the Microsoft login flow.
        method: "totp", "manual", or "auto" (tries totp first, falls back to
                manual if secret not set).
        totp_secret: TOTP secret key string. Required for "totp" mode.
        timeout: Maximum seconds to wait for the MFA prompt to appear and
                 be handled.
        post_submit_timeout: After submitting the code, maximum seconds to
                             wait for the MFA page to clear (login to proceed).

    Returns:
        MfaResult with success, status, and diagnostic information.
    """
    start = asyncio.get_event_loop().time()
    deadline = start + timeout

    # Resolve the effective method
    effective_method: MfaMethod
    if method == "auto":
        if totp_secret:
            effective_method = MfaMethod.TOTP
            logger.info("MFA method=auto resolved to totp (secret provided)")
        else:
            effective_method = MfaMethod.MANUAL
            logger.info("MFA method=auto resolved to manual (no secret)")
    else:
        try:
            effective_method = MfaMethod(method)
        except ValueError:
            return MfaResult(
                success=False,
                status=MfaStatus.ERROR,
                reason=f"Unknown MFA method: {method!r}. Use 'totp', 'manual', or 'auto'.",
            )

    # ------------------------------------------------------------------
    # Phase 1: Wait for an MFA prompt to appear
    # ------------------------------------------------------------------
    logger.info("Waiting for MFA prompt (timeout=%.0fs)...", timeout)

    prompt_type: str | None = None
    while asyncio.get_event_loop().time() < deadline:
        prompt_type = await detect_mfa_prompt(page, timeout_ms=3000)
        if prompt_type is not None:
            logger.info("MFA prompt detected: %s", prompt_type)
            break
        await asyncio.sleep(1)

    if prompt_type is None:
        # No MFA prompt appeared. Check if we're already past MFA —
        # maybe the tenant doesn't require it.
        current_url = page.url
        if "login.microsoftonline.com" not in current_url:
            elapsed = asyncio.get_event_loop().time() - start
            logger.info("No MFA prompt, but already redirected away from login — MFA may not be required")
            return MfaResult(
                success=True,
                status=MfaStatus.SKIPPED,
                reason="No MFA prompt detected; tenant may not require MFA",
                elapsed_seconds=elapsed,
            )

        elapsed = asyncio.get_event_loop().time() - start
        return MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="Timed out waiting for MFA prompt",
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Phase 2: Handle the detected prompt
    # ------------------------------------------------------------------
    remaining = deadline - asyncio.get_event_loop().time()

    result: MfaResult

    if prompt_type == "code_input":
        if effective_method == MfaMethod.TOTP:
            if not totp_secret:
                return MfaResult(
                    success=False,
                    status=MfaStatus.ERROR,
                    reason="TOTP method selected but no totp_secret provided",
                    method_used="totp",
                )
            result = await _handle_totp(page, totp_secret, remaining)
        else:
            result = await _handle_manual(page, remaining)

    elif prompt_type == "push_pending":
        if effective_method == MfaMethod.TOTP:
            # Try to switch from push to code entry
            logger.info("Push notification detected; trying to switch to code entry")
            try:
                code_link = page.locator("#idAADTOTP_Description")
                if await code_link.is_visible(timeout=3000):
                    await code_link.click()
                    logger.info("Switched to code entry from push")
                    # Now handle code input
                    remaining2 = deadline - asyncio.get_event_loop().time()
                    if not totp_secret:
                        return MfaResult(
                            success=False,
                            status=MfaStatus.ERROR,
                            reason="TOTP method selected but no totp_secret provided (after push→code switch)",
                            method_used="totp",
                        )
                    result = await _handle_totp(page, totp_secret, remaining2)
                else:
                    # Can't switch; wait for push approval
                    result = await _handle_push_pending(page, remaining)
            except Exception:
                result = await _handle_push_pending(page, remaining)
        else:
            result = await _handle_push_pending(page, remaining)

    elif prompt_type == "device_select":
        result = await _handle_device_selection(page)
        if result.success:
            # After selecting device, a code_input or push may appear next
            remaining2 = deadline - asyncio.get_event_loop().time()
            new_prompt = await detect_mfa_prompt(page, timeout_ms=5000)
            if new_prompt == "code_input":
                if effective_method == MfaMethod.TOTP:
                    if not totp_secret:
                        return MfaResult(
                            success=False,
                            status=MfaStatus.ERROR,
                            reason="TOTP method selected but no totp_secret provided",
                            method_used="totp",
                        )
                    result = await _handle_totp(page, totp_secret, remaining2)
                else:
                    result = await _handle_manual(page, remaining2)

    elif prompt_type == "verify_identity":
        # This is often a page asking "How would you like to verify?"
        # Try to find and click a code-based option, then loop back.
        logger.info("Generic verify identity prompt — looking for code option")
        result = await _handle_device_selection(page)
        if result.success:
            remaining2 = deadline - asyncio.get_event_loop().time()
            new_prompt = await detect_mfa_prompt(page, timeout_ms=5000)
            if new_prompt == "code_input":
                if effective_method == MfaMethod.TOTP:
                    if not totp_secret:
                        return MfaResult(
                            success=False,
                            status=MfaStatus.ERROR,
                            reason="TOTP method selected but no totp_secret provided",
                            method_used="totp",
                        )
                    result = await _handle_totp(page, totp_secret, remaining2)
                else:
                    result = await _handle_manual(page, remaining2)

    else:
        result = MfaResult(
            success=False,
            status=MfaStatus.UNSUPPORTED_PROMPT,
            reason=f"Unrecognized MFA prompt type: {prompt_type!r}",
            prompt_type=prompt_type,
        )

    if not result.success:
        result.elapsed_seconds = asyncio.get_event_loop().time() - start
        return result

    # ------------------------------------------------------------------
    # Phase 3: Wait for MFA page to clear (login to proceed past MFA)
    # ------------------------------------------------------------------
    logger.info("MFA code submitted; waiting for login to proceed (post-submit timeout=%.0fs)...", post_submit_timeout)
    post_start = asyncio.get_event_loop().time()
    post_deadline = post_start + post_submit_timeout

    while asyncio.get_event_loop().time() < post_deadline:
        prompt_type = await detect_mfa_prompt(page, timeout_ms=3000)
        if prompt_type is None:
            # MFA prompt is gone — check if we moved forward
            current_url = page.url
            if "login.microsoftonline.com" not in current_url or "spa-signin" in current_url:
                logger.info("Login proceeded past MFA (url: %s)", current_url)
                result.elapsed_seconds = asyncio.get_event_loop().time() - start
                return result

        # If a code_input is still visible but shows an error, surface it
        try:
            error_el = page.locator("#idAADTOTP_Description_Error,#idDiv_SAOTCC_ErrorMsg,.error")
            if await error_el.is_visible(timeout=1000):
                error_text = await error_el.text_content()
                result = MfaResult(
                    success=False,
                    status=MfaStatus.ERROR,
                    reason=f"MFA code rejected: {error_text.strip() if error_text else 'unknown error'}",
                    method_used=result.method_used,
                    prompt_type=prompt_type,
                    elapsed_seconds=asyncio.get_event_loop().time() - start,
                )
                return result
        except Exception:
            pass

        await asyncio.sleep(1)

    # Timeout waiting for MFA page to clear
    result.success = False
    result.status = MfaStatus.TIMEOUT
    result.reason = "MFA code was submitted but login did not proceed within post-submit timeout"
    result.elapsed_seconds = asyncio.get_event_loop().time() - start
    return result


# ---------------------------------------------------------------------------
# Convenience: wait for MFA to be over (used by login scripts that want a
# simpler API)
# ---------------------------------------------------------------------------

async def wait_for_mfa_resolution(
    page: "Page",
    *,
    timeout: float = 300.0,
) -> MfaResult:
    """Wait until the MFA page is gone — simpler polling-based API.

    Use this when you don't need to actively submit a code (e.g., push
    notification or already-submitted code) and just want to block until
    MFA completes or times out.

    Args:
        page: Playwright Page object.
        timeout: Maximum seconds to wait.

    Returns:
        MfaResult indicating whether MFA resolved.
    """
    start = asyncio.get_event_loop().time()
    deadline = start + timeout

    while asyncio.get_event_loop().time() < deadline:
        prompt_type = await detect_mfa_prompt(page, timeout_ms=3000)
        if prompt_type is None:
            current_url = page.url
            if "login.microsoftonline.com" not in current_url:
                elapsed = asyncio.get_event_loop().time() - start
                logger.info("MFA resolved — redirected away from login page")
                return MfaResult(
                    success=True,
                    status=MfaStatus.SUCCESS,
                    reason="MFA resolved",
                    elapsed_seconds=elapsed,
                )
        await asyncio.sleep(1)

    elapsed = asyncio.get_event_loop().time() - start
    return MfaResult(
        success=False,
        status=MfaStatus.TIMEOUT,
        reason="MFA did not resolve within timeout",
        elapsed_seconds=elapsed,
    )
