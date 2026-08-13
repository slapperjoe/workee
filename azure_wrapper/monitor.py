"""SessionMonitor — heartbeat loop and session keep-alive.

Monitors an active Azure session for expiration, triggers re-auth,
and tracks session health metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

LOGIN_HOST = "login.microsoftonline.com"

# Session expiration text indicators
SESSION_EXPIRED_INDICATORS = [
    "Your session has expired",
    "Session expired",
    "Sign in to continue",
    "You have been signed out",
]


class SessionMonitor:
    """Periodically check session health and trigger re-auth if needed."""

    def __init__(
        self,
        poll_interval: float = 15.0,
        heartbeat_interval: float = 300.0,
        reauth_timeout: float = 300.0,
    ):
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.reauth_timeout = reauth_timeout
        self._running = False
        self._stop_event = asyncio.Event()

        # Counters
        self.reauth_count = 0
        self.last_reauth_time = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        page: Page,
        context: BrowserContext,
        reauth_cb=None,  # async callable(page, context) -> bool
        heartbeat_cb=None,  # async callable(page, context) -> None
    ) -> None:
        """Run the session keep-alive loop.

        Blocks until stop() is called (from another task) or SIGINT.

        Args:
            page: The dashboard/portal page to monitor.
            context: The browser context.
            reauth_cb: Optional async callback for re-authentication.
                       Called with (page, context), must return True on success.
            heartbeat_cb: Optional async callback called on each healthy
                          heartbeat tick.
        """
        self._running = True
        self._stop_event.clear()
        logger.info(
            "Session monitor started (poll: %.0fs, heartbeat: %.0fs)",
            self.poll_interval,
            self.heartbeat_interval,
        )

        last_heartbeat = time.monotonic()

        while self._running:
            try:
                expired = await self._check_expired(page)
                if expired:
                    logger.warning("Session expired — starting re-authentication")
                    if reauth_cb:
                        ok = await reauth_cb(page, context)
                    else:
                        ok = False
                    self.reauth_count += 1
                    self.last_reauth_time = time.monotonic()
                    if ok:
                        logger.info(
                            "Re-auth #%d complete", self.reauth_count
                        )
                    else:
                        logger.error("Re-auth #%d failed", self.reauth_count)

                # Heartbeat
                now = time.monotonic()
                if now - last_heartbeat >= self.heartbeat_interval:
                    if heartbeat_cb:
                        try:
                            await heartbeat_cb(page, context)
                        except Exception as exc:
                            logger.debug("Heartbeat callback error: %s", exc)
                    last_heartbeat = now

                # Wait for poll interval or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

            except Exception as exc:
                logger.exception("Error in session monitor: %s", exc)
                await asyncio.sleep(5)

        logger.info("Session monitor stopped")

    def stop(self) -> None:
        """Signal the monitor loop to exit."""
        self._running = False
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _check_expired(self, page: Page) -> bool:
        """Check if the session has expired (redirected to login or
        page shows expiration messaging).
        """
        if not page or page.is_closed():
            return True

        try:
            url = page.url

            # Primary: redirect to login
            if LOGIN_HOST in url:
                logger.info("Session expired — redirected to Microsoft login")
                return True

            # Secondary: inline expiration messages
            for text in SESSION_EXPIRED_INDICATORS:
                try:
                    locator = page.get_by_text(text, exact=False)
                    if await locator.count() > 0:
                        el = locator.first
                        if await el.is_visible():
                            logger.info(
                                "Session expired — detected: %s", text
                            )
                            return True
                except Exception:
                    continue

            # Tertiary: page is on neither AVD nor portal domain
            if (
                "windows.cloud.microsoft" not in url
                and "portal.azure.com" not in url
                and LOGIN_HOST not in url
            ):
                logger.debug("Page is on unknown domain: %s", url[:100])

        except Exception as exc:
            logger.debug("Error checking session health: %s", exc)

        return False
