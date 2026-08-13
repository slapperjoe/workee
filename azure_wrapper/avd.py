"""AVDSession — Azure Virtual Desktop web client backend.

VM listing via feed discovery API at rdweb.wvd.microsoft.com.
VM connection via RDP WebAssembly in new browser tab.

Evolution of the existing avd_session.py AVDSessionManager, now
conforming to the AzureSession ABC with launchPersistentContext and
signal/wait MFA.
"""

from __future__ import annotations

import asyncio
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

ENTRY_URL = "https://windows.microsoft.cloud"
DASHBOARD_URL = "https://windows.cloud.microsoft/#/devices"
CLIENT_ID = "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"
SCOPE = "https://www.wvd.microsoft.com/User.Access"
FEED_API = "https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery"

# VM card/tile selectors — ordered by specificity
VM_SELECTORS = [
    '[data-testid*="resource"]',
    '[class*="ResourceCard"]',
    '[class*="resource-card"]',
    'a[href*="/webclient/"]',
    'a[href*="devices"]',
    '[role="listitem"] a',
    '[role="link"]',
]


class AVDSession(AzureSession):
    """Session targeting the Azure Virtual Desktop web client.

    VM listing via feed discovery API.
    VM connection via RDP WebAssembly in new browser tab.
    """

    def __init__(self, config: AzureConfig):
        if config.backend != "avd":
            config.backend = "avd"
        super().__init__(config)

    # ------------------------------------------------------------------
    # VM operations
    # ------------------------------------------------------------------

    async def get_vms(self) -> list[VmInfo]:
        """Query feed discovery API for provisioned desktops/apps.

        Falls back to DOM scraping if the API call fails.
        """
        if not self._page or self._page.is_closed():
            logger.error("No active page")
            return []

        await self._ensure_on_vm_list()

        # Try feed discovery API first
        try:
            feed_data = await self._page.evaluate("""
                async () => {
                    const resp = await fetch(
                        'https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery',
                        { credentials: 'include' }
                    );
                    if (!resp.ok) return null;
                    return await resp.json();
                }
            """)
            if feed_data:
                vms = self._parse_feed_response(feed_data)
                if vms:
                    self._vms = vms
                    return vms
        except Exception as exc:
            logger.debug("Feed discovery API failed: %s", exc)

        # Fall back to DOM scraping
        vms = await self._scrape_vms_from_dom(self._page)
        self._vms = vms
        return vms

    async def connect(
        self, vm_id: str, method: str = "auto"
    ) -> VmConnection:
        """Open AVD web client session for a VM.

        Args:
            vm_id: AVD resource ID (GUID) or VM name for fuzzy matching.
            method: Ignored for AVD (always "avd_rdp").

        Returns:
            VmConnection handle wrapping the new RDP tab.
        """
        if not self._page or self._page.is_closed():
            raise RuntimeError("No active page — cannot connect")

        await self._ensure_on_vm_list()
        await asyncio.sleep(3)

        logger.info("Looking for VM: %s", vm_id)

        # Strategy 1: If vm_id looks like a GUID, navigate directly to webclient
        if self._is_guid(vm_id):
            webclient_url = (
                "https://windows.cloud.microsoft/webclient/index.html"
                f"?resourceId={vm_id}"
            )
            logger.info("Navigating to webclient: %s", webclient_url)
            new_page = await self._context.new_page()
            await new_page.goto(webclient_url, wait_until="domcontentloaded")
            await self.configure_connection_page(new_page)
            conn = VmConnection(
                vm_id=vm_id,
                vm_name=vm_id,
                backend="avd",
                method="avd_rdp",
                page=new_page,
                connected_at=time.monotonic(),
            )
            self._connections.append(conn)
            return conn

        # Strategy 2: Find VM by name on dashboard, click to open new tab
        clicked = await self._click_vm_by_text(vm_id)
        if clicked:
            # Check for new tab
            pages = self._context.pages if self._context else []
            conn_page = None
            for p in pages:
                if p != self._page and not p.is_closed():
                    if "webclient" in (p.url or ""):
                        conn_page = p
                        break
            if not conn_page and len(pages) > 1:
                conn_page = pages[-1]

            conn = VmConnection(
                vm_id=vm_id,
                vm_name=vm_id,
                backend="avd",
                method="avd_rdp",
                page=conn_page,
                connected_at=time.monotonic(),
            )
            self._connections.append(conn)
            return conn

        # Strategy 3: Fuzzy match from VM list, use resource ID to build URL
        vms = await self.get_vms()
        for vm in vms:
            if vm_id.lower() in vm.name.lower():
                if vm.id:
                    webclient_url = (
                        "https://windows.cloud.microsoft/webclient/index.html"
                        f"?resourceId={vm.id}"
                    )
                    logger.info("Navigating to webclient: %s", webclient_url)
                    new_page = await self._context.new_page()
                    await new_page.goto(
                        webclient_url, wait_until="domcontentloaded"
                    )
                    await self.configure_connection_page(new_page)
                    conn = VmConnection(
                        vm_id=vm.id,
                        vm_name=vm.name,
                        backend="avd",
                        method="avd_rdp",
                        page=new_page,
                        connected_at=time.monotonic(),
                    )
                    self._connections.append(conn)
                    return conn

        raise RuntimeError(f"Could not find VM: {vm_id}")

    # ------------------------------------------------------------------
    # Backend-specific abstract methods
    # ------------------------------------------------------------------

    async def _navigate_to_vm_list(self) -> None:
        """Navigate to the AVD dashboard."""
        if self._page:
            await self._page.goto(
                DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(5)
            try:
                await self._page.wait_for_load_state(
                    "networkidle", timeout=15000
                )
            except Exception:
                pass
            logger.info("Dashboard loaded: %s", self._page.url[:100])

    async def _is_on_vm_list(self, page: Page) -> bool:
        """Check if we're on the AVD dashboard."""
        try:
            url = page.url
            return (
                "login.microsoftonline.com" not in url
                and "windows.cloud.microsoft" in url
            )
        except Exception:
            return False

    async def _fetch_vms(self, page: Page) -> list[VmInfo]:
        """Fetch VMs via feed discovery API."""
        try:
            feed_data = await page.evaluate("""
                async () => {
                    const resp = await fetch(
                        'https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery',
                        { credentials: 'include' }
                    );
                    if (!resp.ok) return null;
                    return await resp.json();
                }
            """)
            if feed_data:
                return self._parse_feed_response(feed_data)
        except Exception as exc:
            logger.debug("Feed API failed: %s", exc)
        return await self._scrape_vms_from_dom(page)

    # ------------------------------------------------------------------
    # Internal: VM interactions
    # ------------------------------------------------------------------

    async def _ensure_on_vm_list(self) -> None:
        """Navigate to the dashboard if not already there."""
        if not await self._is_on_vm_list(self._page):
            await self._navigate_to_vm_list()

    async def _click_vm_by_text(self, vm_name: str) -> bool:
        """Find a VM on the dashboard by name and click it.

        Returns True if a click was performed.
        """
        page = self._page
        if not page:
            return False

        logger.debug("Searching DOM for VM: %s", vm_name)

        # Strategy A: text locator
        try:
            text_locator = page.get_by_text(vm_name, exact=False)
            count = await text_locator.count()
            for i in range(min(count, 10)):
                el = text_locator.nth(i)
                if await el.is_visible():
                    try:
                        await el.click(timeout=5000)
                        await asyncio.sleep(3)
                        return True
                    except Exception:
                        # Try parent
                        try:
                            parent = el.locator("..")
                            if await parent.count() > 0:
                                await parent.first.click(timeout=5000)
                                await asyncio.sleep(3)
                                return True
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug("Text locator search failed: %s", exc)

        # Strategy B: VM selectors
        for sel in VM_SELECTORS:
            try:
                elements = page.locator(sel)
                count = await elements.count()
                for i in range(min(count, 20)):
                    el = elements.nth(i)
                    if await el.is_visible():
                        try:
                            text = await el.text_content()
                            if text and vm_name.lower() in text.lower():
                                await el.click(timeout=5000)
                                await asyncio.sleep(3)
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        return False

    async def _scrape_vms_from_dom(self, page: Page) -> list[VmInfo]:
        """Scrape VM names from the dashboard DOM."""
        vms: list[VmInfo] = []
        for sel in VM_SELECTORS:
            try:
                elements = page.locator(sel)
                count = await elements.count()
                for i in range(min(count, 30)):
                    el = elements.nth(i)
                    if await el.is_visible():
                        try:
                            text = (await el.text_content()).strip()
                            href = await el.get_attribute("href") or ""
                            if text and len(text) > 1:
                                vms.append(VmInfo(
                                    id=self._extract_guid(href),
                                    name=text.split("\n")[0][:100],
                                    backend="avd",
                                    kind="desktop",
                                    workspace="",
                                ))
                        except Exception:
                            continue
                if vms:
                    break
            except Exception:
                continue
        return vms

    # ------------------------------------------------------------------
    # Internal: feed parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_feed_response(data: dict | list) -> list[VmInfo]:
        """Parse feed discovery API response into VmInfo list."""
        vms: list[VmInfo] = []

        # Handle plain list
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if "resources" in item:
                        workspace = item.get("workspace", {})
                        workspace_name = workspace.get(
                            "name", workspace.get("friendlyName", "Unknown")
                        )
                        for resource in item.get("resources", []):
                            vms.append(VmInfo(
                                id=resource.get("resourceId", resource.get("id", "")),
                                name=resource.get(
                                    "name",
                                    resource.get("friendlyName", "Unnamed"),
                                ),
                                backend="avd",
                                kind=resource.get(
                                    "resourceType",
                                    resource.get("type", "unknown"),
                                ),
                                workspace=workspace_name,
                            ))
                    else:
                        vms.append(VmInfo(
                            id=item.get("resourceId", item.get("id", "")),
                            name=item.get("name", item.get("friendlyName", "Unnamed")),
                            backend="avd",
                            kind=item.get("resourceType", item.get("type", "unknown")),
                            workspace=item.get("workspaceName", ""),
                        ))
            return vms

        # Standard response: {"value": [...]}
        resources_list = data.get("value", []) if isinstance(data, dict) else []

        for workspace_entry in resources_list:
            workspace = workspace_entry.get("workspace", {})
            workspace_name = workspace.get(
                "name", workspace.get("friendlyName", "Unknown")
            )
            for resource in workspace_entry.get("resources", []):
                vms.append(VmInfo(
                    id=resource.get("resourceId", resource.get("id", "")),
                    name=resource.get("name", resource.get("friendlyName", "Unnamed")),
                    backend="avd",
                    kind=resource.get("resourceType", resource.get("type", "unknown")),
                    workspace=workspace_name,
                ))
        return vms

    @staticmethod
    def _is_guid(s: str) -> bool:
        """Check if a string looks like a GUID."""
        return bool(re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            s,
        ))

    @staticmethod
    def _extract_guid(text: str) -> str:
        """Extract a GUID from a string."""
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{12}",
            text,
        )
        return match.group(0) if match else ""
