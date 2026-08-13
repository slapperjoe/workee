"""PortalSession — Azure Portal backend for VM access.

VM listing via ARM REST API (management.azure.com).
VM connection via Bastion (new tab) or Serial Console (embedded blade).

NEW backend — complements the AVD backend for infrastructure VM access.
Requires the browser to be authenticated against portal.azure.com.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from azure_wrapper.config import AzureConfig
from azure_wrapper.session import AzureSession
from azure_wrapper.types import VmConnection, VmInfo

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

PORTAL_ENTRY_URL = "https://portal.azure.com/"
VM_LIST_URL = (
    "https://portal.azure.com/#browse/Microsoft.Compute/virtualMachines"
)
PORTAL_CLIENT_ID = "c44b4083-3bb0-49c1-b47d-974e53cbdf3c"
ARM_SCOPE = "https://management.core.windows.net//.default"
ARM_API = "https://management.azure.com"
ARM_API_VERSION = "2026-03-02"


class PortalSession(AzureSession):
    """Session targeting the Azure portal (portal.azure.com).

    VM listing via ARM REST API.
    VM connection via Bastion (new tab) or Serial Console.
    """

    def __init__(self, config: AzureConfig):
        if config.backend != "portal":
            config.backend = "portal"
        super().__init__(config)

    # ------------------------------------------------------------------
    # VM operations
    # ------------------------------------------------------------------

    async def get_vms(self) -> list[VmInfo]:
        """Query ARM REST API for VMs in the configured subscription.

        Extracts the Bearer token from the browser's MSAL localStorage cache,
        then calls the ARM API. Falls back to DOM scraping.
        """
        if not self._page or self._page.is_closed():
            logger.error("No active page")
            return []

        await self._ensure_on_vm_list()

        # Try ARM API
        try:
            token = await self.auth.extract_access_token(
                self._page, PORTAL_CLIENT_ID
            )
            if not token:
                logger.debug("No portal access token — trying feed API fallback")
                token = await self.auth.extract_access_token(
                    self._page, "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"  # AVD client ID
                )

            if token:
                vms = await self._fetch_vms_via_arm(token)
                if vms:
                    self._vms = vms
                    return vms
        except Exception as exc:
            logger.debug("ARM API VM listing failed: %s", exc)

        # Fall back to DOM scraping
        vms = await self._scrape_vms_from_portal(self._page)
        self._vms = vms
        return vms

    async def connect(
        self, vm_id: str, method: str = "bastion"
    ) -> VmConnection:
        """Open a remote session to the VM via Bastion or Serial Console.

        Args:
            vm_id: ARM resource ID of the VM (e.g.,
                   /subscriptions/{sub}/resourceGroups/{rg}/
                   providers/Microsoft.Compute/virtualMachines/{name}).
            method: "bastion" (default) or "serial_console".

        Returns:
            VmConnection handle.
        """
        if not self._page or self._page.is_closed():
            raise RuntimeError("No active page — cannot connect")

        if method == "serial_console":
            return await self._connect_serial(self._page, vm_id)
        else:
            return await self._connect_bastion(self._page, vm_id)

    async def start_vm(self, vm_id: str) -> bool:
        """Start a VM via the ARM REST API.

        Args:
            vm_id: ARM resource ID of the VM to start.

        Returns:
            True if the start request was accepted (202).
        """
        if not self._page or self._page.is_closed():
            raise RuntimeError("No active page — cannot start VM")

        token = await self.auth.extract_access_token(self._page, PORTAL_CLIENT_ID)
        if not token:
            raise RuntimeError("No access token available — re-authenticate first")

        url = (
            f"{ARM_API}{vm_id}/start"
            f"?api-version={ARM_API_VERSION}"
        )

        logger.info("Starting VM: %s", url)
        response = await self._page.request.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Length": "0",
            },
        )

        if response.status in (200, 202):
            logger.info("VM start accepted (status %d)", response.status)
            return True

        body = (await response.text())[:500]
        logger.error("VM start failed (%d): %s", response.status, body)
        raise RuntimeError(f"VM start failed: HTTP {response.status} — {body}")

    # ------------------------------------------------------------------
    # Backend-specific abstract methods
    # ------------------------------------------------------------------

    async def _navigate_to_vm_list(self) -> None:
        """Navigate to the Azure Portal VM list."""
        if self._page:
            await self._page.goto(
                PORTAL_ENTRY_URL, wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(3)
            # Navigate to VM browse blade
            await self._page.goto(
                VM_LIST_URL, wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(5)
            try:
                await self._page.wait_for_load_state(
                    "networkidle", timeout=15000
                )
            except Exception:
                pass
            logger.info("Portal VM list loaded: %s", self._page.url[:100])

    async def _is_on_vm_list(self, page: Page) -> bool:
        """Check if we're on the Azure Portal VM browse page."""
        try:
            url = page.url
            return (
                "login.microsoftonline.com" not in url
                and "portal.azure.com" in url
            )
        except Exception:
            return False

    async def _fetch_vms(self, page: Page) -> list[VmInfo]:
        """Fetch VMs from the ARM API."""
        token = await self.auth.extract_access_token(page, PORTAL_CLIENT_ID)
        if token:
            return await self._fetch_vms_via_arm(token)
        return await self._scrape_vms_from_portal(page)

    # ------------------------------------------------------------------
    # Internal: ARM API
    # ------------------------------------------------------------------

    async def _fetch_vms_via_arm(self, token: str) -> list[VmInfo]:
        """Call the ARM API to list VMs.

        Uses page.request for cookie-authenticated requests with the
        bearer token in the Authorization header.
        """
        subscription_id = self.config.subscription_id
        if not subscription_id:
            logger.warning(
                "No AZURE_SUBSCRIPTION_ID configured; cannot call ARM API"
            )
            return []

        url = (
            f"{ARM_API}/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/virtualMachines"
            f"?api-version={ARM_API_VERSION}"
        )

        logger.info("Fetching VMs from ARM API: %s", url)
        response = await self._page.request.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

        if not response.ok:
            logger.warning(
                "ARM API returned %d: %s",
                response.status,
                (await response.text())[:500],
            )
            return []

        data = await response.json()
        return self._parse_arm_response(data)

    @staticmethod
    def _parse_arm_response(data: dict) -> list[VmInfo]:
        """Parse ARM VM list response into VmInfo."""
        vms: list[VmInfo] = []

        for vm in data.get("value", []):
            props = vm.get("properties", {})
            instance_view = vm.get("properties", {}).get("instanceView", {})

            # Parse power state
            power_state = ""
            for status in instance_view.get("statuses", []):
                code = status.get("code", "")
                if code.startswith("PowerState/"):
                    power_state = code.split("/", 1)[1]
                    break

            vms.append(VmInfo(
                id=vm.get("id", ""),
                name=vm.get("name", "Unnamed"),
                backend="portal",
                kind="virtualMachine",
                workspace="",
                location=vm.get("location", ""),
                power_state=power_state,
            ))

        return vms

    # ------------------------------------------------------------------
    # Internal: Bastion
    # ------------------------------------------------------------------

    async def _connect_bastion(self, page: Page, vm_id: str) -> VmConnection:
        """Navigate to the VM overview blade and initiate Bastion connect.

        Steps:
        1. Navigate to VM overview blade
        2. Click "Connect" → "Bastion"
        3. Set up a listener for new tab
        4. Return VmConnection wrapping the new tab
        """
        logger.info("Connecting to VM via Bastion: %s", vm_id)

        # Navigate to VM overview
        overview_url = (
            f"https://portal.azure.com/#resource{vm_id}/overview"
        )
        await page.goto(overview_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # Set up new tab listener BEFORE clicking Connect
        if self._context is None:
            raise RuntimeError("No browser context")

        new_page_future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _on_page(new_page):
            if not new_page_future.done():
                new_page_future.set_result(new_page)
                logger.info("New tab opened: %s", new_page.url[:100])

        self._context.on("page", _on_page)

        try:
            # Click "Connect" button
            connect_btn = page.get_by_text("Connect", exact=False).first
            if await connect_btn.is_visible(timeout=5000):
                await connect_btn.click()
                await asyncio.sleep(2)

                # Click "Bastion" option
                bastion_btn = page.get_by_text("Bastion", exact=False).first
                if await bastion_btn.is_visible(timeout=5000):
                    await bastion_btn.click()
                    await asyncio.sleep(1)

                    # Look for "Connect" / "Use Bastion" button in the blade
                    use_bastion = page.get_by_text(
                        "Use Bastion", exact=False
                    ).first
                    if await use_bastion.is_visible(timeout=3000):
                        await use_bastion.click()

            # Wait for the new tab to open (Bastion HTML5 client)
            try:
                new_page = await asyncio.wait_for(new_page_future, timeout=30.0)
                await self.configure_connection_page(new_page)
                conn = VmConnection(
                    vm_id=vm_id,
                    vm_name=vm_id.split("/")[-1] if "/" in vm_id else vm_id,
                    backend="portal",
                    method="bastion",
                    page=new_page,
                    connected_at=time.monotonic(),
                )
                self._connections.append(conn)
                return conn
            except asyncio.TimeoutError:
                logger.warning("No new tab opened within timeout for Bastion")
                # Return a connection with the current page as fallback
                conn = VmConnection(
                    vm_id=vm_id,
                    vm_name=vm_id.split("/")[-1] if "/" in vm_id else vm_id,
                    backend="portal",
                    method="bastion",
                    page=page,
                    connected_at=time.monotonic(),
                )
                self._connections.append(conn)
                return conn
        finally:
            try:
                self._context.remove_listener("page", _on_page)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal: Serial Console
    # ------------------------------------------------------------------

    async def _connect_serial(self, page: Page, vm_id: str) -> VmConnection:
        """Navigate to Serial Console for the VM."""
        logger.info("Connecting to VM via Serial Console: %s", vm_id)

        # Navigate to serial console blade
        serial_url = (
            f"https://portal.azure.com/#resource{vm_id}/serialConsole"
        )
        await page.goto(serial_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(10)

        # Serial Console for Linux VMs uses "SAC" (Special Admin Console)
        conn = VmConnection(
            vm_id=vm_id,
            vm_name=vm_id.split("/")[-1] if "/" in vm_id else vm_id,
            backend="portal",
            method="serial_console",
            page=page,
            connected_at=time.monotonic(),
        )
        self._connections.append(conn)
        return conn

    # ------------------------------------------------------------------
    # Internal: DOM scraping
    # ------------------------------------------------------------------

    async def _scrape_vms_from_portal(self, page: Page) -> list[VmInfo]:
        """Fallback: scrape VM names from the portal DOM."""
        vms: list[VmInfo] = []

        selectors_to_try = [
            '[class*="virtualMachine"]',
            '[class*="azc-grid-cell"]',
            '[class*="listitem"]',
            '[role="gridcell"]',
            'a[href*="/virtualMachines/"]',
        ]

        for sel in selectors_to_try:
            try:
                elements = page.locator(sel)
                count = await elements.count()
                for i in range(min(count, 50)):
                    el = elements.nth(i)
                    if await el.is_visible():
                        try:
                            text = (await el.text_content()).strip()
                            href = await el.get_attribute("href") or ""
                            if text and len(text) > 1:
                                vms.append(VmInfo(
                                    id=self._extract_arm_id(href) or text[:50],
                                    name=text.split("\n")[0][:100],
                                    backend="portal",
                                    kind="virtualMachine",
                                    workspace="",
                                ))
                        except Exception:
                            continue
                if vms:
                    break
            except Exception:
                continue

        return vms

    async def _ensure_on_vm_list(self) -> None:
        """Navigate to the portal VM list if not already there."""
        if not await self._is_on_vm_list(self._page):
            await self._navigate_to_vm_list()

    @staticmethod
    def _extract_arm_id(text: str) -> str:
        """Extract an ARM resource ID from text.

        e.g., /subscriptions/.../virtualMachines/vm-name
        """
        match = re.search(
            r"/subscriptions/[^\s\"'>]+", text
        )
        return match.group(0) if match else ""
