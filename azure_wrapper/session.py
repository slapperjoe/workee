"""AzureSession — abstract base class for persistent Chromium Azure sessions.

Provides the shared browser lifecycle, authentication, MFA coordination,
session monitoring, and VM connection management.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright

from azure_wrapper.auth import AuthManager, AuthPhase
from azure_wrapper.config import AzureConfig
from azure_wrapper.mfa import MfaManager, MfaStatus
from azure_wrapper.monitor import SessionMonitor
from azure_wrapper.qr_auth import QrAuthConfig, QrAuthManager, QrAuthServer
from azure_wrapper.resolution import get_resolution
from azure_wrapper.types import QrAuthToken, SessionState, VmConnection, VmInfo

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


class AzureSession(ABC):
    """Persistent Chromium session for Azure VM remote access.

    Manages browser lifecycle, authentication, session keep-alive,
    VM listing, and VM connection. Backend-specific subclasses
    implement the VM operations.

    Usage:
        session = AVDSession(config)  # or PortalSession(config)
        await session.start()

        if session.mfa_pending:
            code = input("Enter MFA code: ")
            session.provide_mfa_code(code)

        vms = await session.get_vms()
        conn = await session.connect(vms[0].id)
        await session.run_loop()  # keep alive until Ctrl+C
    """

    def __init__(self, config: AzureConfig):
        self.config = config
        self.auth = AuthManager(config)

        # QR code auth setup (Australia government compliance)
        qr_config: QrAuthConfig | None = None
        qr_manager: QrAuthManager | None = None
        if config.mfa_method == "qr" or config.qr_signing_key:
            qr_config = QrAuthConfig(
                signing_key=config.qr_signing_key,
                token_ttl=config.qr_token_ttl,
                callback_host=config.qr_callback_host,
                callback_port=config.qr_callback_port,
                callback_path=config.qr_callback_path,
            )
            qr_manager = QrAuthManager(qr_config)

        self.mfa = MfaManager(
            method=config.mfa_method,
            totp_secret=config.totp_secret,
            timeout=config.mfa_timeout,
            post_submit_timeout=config.mfa_post_submit_timeout,
            mfa_hook=config.__dict__.get("mfa_hook"),
            qr_manager=qr_manager,
            qr_config=qr_config,
        )
        self._qr_manager = qr_manager
        self._qr_config = qr_config
        self.monitor = SessionMonitor(
            poll_interval=config.session_poll_interval,
            heartbeat_interval=config.heartbeat_interval,
            reauth_timeout=config.reauth_timeout,
        )

        # Browser
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Runtime
        self._start_time: float = 0.0
        self._vms: list[VmInfo] = []
        self._connections: list[VmConnection] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Launch browser, authenticate, navigate to VM list.

        Returns True if authentication succeeded (possibly after MFA).
        If MFA is needed and can't be resolved automatically, sets
        mfa_pending = True and returns True — the caller must then call
        provide_mfa_code().
        """
        self._start_time = time.monotonic()

        logger.info("Starting Azure session (backend: %s)", self.config.backend)
        self._pw = await async_playwright().start()

        # Resolve viewport dynamically from config + runtime override
        # Uses full priority chain: configured → client viewport → system auto-detect → fallback
        viewport = get_resolution(
            configured=self.config.resolution,
            client_viewport=getattr(self.config, "_client_viewport", None),
            fallback=self.config.viewport,
        )

        if self.config.persistence_mode == "persistent_context":
            self._context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                headless=not self.config.headed,
                user_agent=self.config.user_agent,
                viewport=viewport,
                args=self.config.chromium_args,
            )
            self._browser = self._context.browser  # type: ignore[assignment]
        else:
            self._browser = await self._pw.chromium.launch(
                headless=not self.config.headed,
                args=self.config.chromium_args,
            )
            self._context = await self._browser.new_context(
                user_agent=self.config.user_agent,
                viewport=viewport,
            )

        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

        # --- Two-phase authentication ---
        # Phase 1: authenticate() handles navigation, email entry, and
        # detects whether the password field or an MFA/QR prompt is next.
        # On password-first tenants: email → password → return PASSWORD_ENTERED.
        # On MFA-first tenants:   email → return MFA_BEFORE_PASSWORD.
        phase = await self.auth.authenticate(
            self._browser, self._context, self._page, reuse_state=True
        )

        if phase == AuthPhase.FAILED:
            logger.error("Authentication failed — could not reach login page")
            return False

        if phase == AuthPhase.DONE:
            # Already authenticated (session reuse or federated bypass).
            # Navigate straight to VM list — no MFA needed.
            await self._navigate_to_vm_list()
            return True

        # --- Handle pre-password MFA if the tenant requires it ---
        # MFA_BEFORE_PASSWORD: the tenant showed an MFA/QR prompt after
        # email instead of the password field. Resolve MFA first, then
        # enter the password.
        if phase == AuthPhase.MFA_BEFORE_PASSWORD:
            logger.info("MFA/QR required before password — handling MFA first")
            mfa_result = await self.mfa.handle(self._page)

            if not mfa_result.success:
                if self.mfa.mfa_pending:
                    # Caller needs to provide MFA code or scan QR.
                    # NOTE: QR code at this stage is the Microsoft Entra ID
                    # MFA page, NOT the local government compliance QR.
                    # The local QR is only generated AFTER password entry.
                    logger.info(
                        "Pre-password MFA pending — caller must call "
                        "provide_mfa_code() or resolve MFA"
                    )
                    return True  # Session is alive, just needs MFA

                logger.error(
                    "Pre-password MFA handling failed: [%s] %s",
                    mfa_result.status.value,
                    mfa_result.reason,
                )
                return False

            # MFA resolved — now the password field should appear.
            phase = await self.auth.enter_password(self._page)
            if phase == AuthPhase.FAILED:
                logger.error("Password entry failed after MFA resolution")
                return False

        elif phase == AuthPhase.PASSWORD_ENTERED:
            # Password was already entered by authenticate() on
            # password-first tenants. No action needed here.
            logger.info("Password entered — proceeding to post-password MFA check")

        elif phase == AuthPhase.NEEDS_PASSWORD:
            # Password field is visible but wasn't auto-filled (rare path;
            # authenticate() normally fills it). Explicitly enter now.
            phase = await self.auth.enter_password(self._page)
            if phase == AuthPhase.FAILED:
                logger.error("Password entry failed")
                return False

        # --- Phase 2: Handle post-password MFA ---
        # At this point, the password has been entered successfully.
        # Now check whether Microsoft requires MFA (code, push, QR, etc.).
        # The QR code (if mfa_method='qr') is ONLY displayed now — AFTER
        # successful password entry — ensuring password is always prompted
        # before any QR code display.
        logger.info("Checking for post-password MFA...")
        mfa_result = await self.mfa.handle(self._page)

        if not mfa_result.success:
            if self.mfa.mfa_pending:
                # Caller needs to provide MFA code or scan QR code.
                # The QR login URL is accessible via session.qr_login_url.
                logger.info(
                    "MFA pending (post-password) — caller must call "
                    "provide_mfa_code() or check qr_login_url"
                )
                return True  # Session is alive, just needs MFA

            if mfa_result.status == MfaStatus.QR_PENDING:
                # QR code detected but QR auth not configured
                logger.warning(
                    "QR code prompt detected but QR auth not configured. "
                    "Set mfa_method='qr' and provide qr_signing_key."
                )
                return False

            logger.error(
                "Post-password MFA handling failed: [%s] %s",
                mfa_result.status.value,
                mfa_result.reason,
            )
            return False

        # Navigate to VM list
        await self._navigate_to_vm_list()
        return True

    async def stop(self) -> None:
        """Save session state and close browser cleanly."""
        logger.info("Stopping Azure session")
        self.monitor.stop()

        # Close all VM connections
        for conn in self._connections:
            await conn.close()
        self._connections.clear()

        if self._context:
            try:
                # Save state in storage_state mode
                if self.config.persistence_mode == "storage_state":
                    await self._context.storage_state(
                        path=str(self.config.storage_state_path)
                    )
                    logger.info(
                        "Saved auth state to %s",
                        self.config.storage_state_path,
                    )
            except Exception as exc:
                logger.warning("Could not save auth state: %s", exc)

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

        logger.info("Azure session closed")

    async def run_loop(self) -> None:
        """Block until interrupted, keeping the session alive.

        Periodically checks for expiration and re-authenticates.
        Runs until stop() is called from another task or SIGINT.
        """
        async def _reauth_cb(page, context):
            result = await self.auth.reauthenticate(context, page)
            # Monitor expects bool; AuthPhase.DONE is success, others are not.
            return result == AuthPhase.DONE

        async def _heartbeat_cb(page, context):
            # Refresh the page to exercise the access token
            if not page.is_closed():
                await page.reload()

        await self.monitor.run(
            self._page,
            self._context,
            reauth_cb=_reauth_cb,
            heartbeat_cb=_heartbeat_cb,
        )

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ------------------------------------------------------------------
    # MFA (signal/wait pattern)
    # ------------------------------------------------------------------

    @property
    def mfa_pending(self) -> bool:
        """True when MFA is required and waiting for user input."""
        return self.mfa.mfa_pending

    @property
    def mfa_event(self) -> asyncio.Event:
        """Event set when MFA resolution is needed.

        Await this, collect user input, then call provide_mfa_code().
        """
        return self.mfa.mfa_event

    def provide_mfa_code(self, code: str) -> None:
        """Submit a user-provided MFA code to the login page.

        Call after mfa_event is set and the user has provided a code.
        """
        self.mfa.provide_code(code)

    def provide_mfa_approval(self) -> None:
        """Signal that the user approved a push notification."""
        self.mfa.provide_approval()

    # ------------------------------------------------------------------
    # QR code auth (Australian government compliance)
    # ------------------------------------------------------------------

    @property
    def qr_login_url(self) -> str:
        """URL of the QR code login page, if QR auth is active."""
        return self.mfa.qr_login_url

    @property
    def qr_auth_token(self) -> str | None:
        """The validated QR auth token, or None."""
        if self._qr_manager:
            return self._qr_manager.auth_token
        return None

    async def cancel_qr_auth(self) -> None:
        """Cancel the QR code auth flow and stop the callback server."""
        await self.mfa.cancel_qr()

    # ------------------------------------------------------------------
    # VM operations (backend-specific)
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_vms(self) -> list[VmInfo]:
        """Return available VMs from the current backend."""

    @abstractmethod
    async def connect(
        self, vm_id: str, method: str = "auto"
    ) -> VmConnection:
        """Open a remote session to the specified VM.

        Args:
            vm_id: Backend-specific resource ID.
            method: Connection method ("auto", "avd_rdp", "bastion",
                    "serial_console").

        Returns:
            VmConnection handle for monitoring the session.
        """

    async def start_vm(self, vm_id: str) -> bool:
        """Start a stopped VM. Default no-op; override in backend.

        Returns True if the start was initiated successfully.
        """
        logger.info("start_vm not implemented for backend %s", self.config.backend)
        return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_state(self) -> SessionState:
        """Return a snapshot of the session's current state."""
        url = ""
        on_vm_list = False
        authenticated = False

        if self._page and not self._page.is_closed():
            try:
                url = self._page.url
                on_vm_list = await self._is_on_vm_list(self._page)
                authenticated = url and "login.microsoftonline.com" not in url
            except Exception:
                pass

        return SessionState(
            authenticated=authenticated,
            on_vm_list=on_vm_list,
            current_url=url,
            vm_count=len(self._vms),
            backend=self.config.backend,
            mfa_pending=self.mfa.mfa_pending,
            reauth_count=self.monitor.reauth_count,
            last_reauth_time=self.monitor.last_reauth_time,
            uptime_seconds=time.monotonic() - self._start_time,
        )

    async def configure_connection_page(self, page: Page) -> None:
        """Apply the current host-reported viewport to a VM connection tab."""
        viewport = get_resolution(
            configured=self.config.resolution,
            client_viewport=getattr(self.config, "_client_viewport", None),
            fallback=self.config.viewport,
        )
        try:
            await page.set_viewport_size(viewport)
        except Exception:
            logger.debug("Could not resize connection page", exc_info=True)

    @property
    def page(self) -> Page | None:
        """The main browser page (dashboard/portal)."""
        return self._page

    @property
    def context(self) -> BrowserContext | None:
        """The browser context."""
        return self._context

    # ------------------------------------------------------------------
    # Abstract: backend-specific
    # ------------------------------------------------------------------

    @abstractmethod
    async def _navigate_to_vm_list(self) -> None:
        """Navigate to the VM listing page for this backend."""

    @abstractmethod
    async def _is_on_vm_list(self, page: Page) -> bool:
        """Return True if the page is showing the VM list."""

    @abstractmethod
    async def _fetch_vms(self, page: Page) -> list[VmInfo]:
        """Fetch VM list from the backend's API or DOM."""
