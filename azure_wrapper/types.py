"""Shared data types for the Azure Chromium wrapper."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class Credentials:
    """Login credentials."""
    email: str
    password: str


@dataclass
class VmInfo:
    """Normalized VM descriptor across backends."""
    id: str = ""                # Backend-specific resource ID
    name: str = ""              # Display name
    backend: str = ""           # "avd" or "portal"
    kind: str = ""              # "desktop", "app", "virtualMachine"
    workspace: str = ""         # AVD workspace name (empty for portal)
    location: str = ""          # Azure region (portal only)
    power_state: str = ""       # "running", "stopped", etc. (portal only)


@dataclass
class VmConnection:
    """Handle to an active VM remote session."""
    vm_id: str
    vm_name: str
    backend: str
    method: str                 # "avd_rdp", "bastion", "serial_console"
    page: Page | None = None    # Playwright Page for the connection tab
    connected_at: float = 0.0

    async def wait_for_disconnect(self) -> None:
        """Resolve when the connection tab closes."""
        if self.page:
            try:
                await self.page.wait_for_event("close", timeout=0)
            except Exception:
                pass

    async def close(self) -> None:
        """Close the connection tab."""
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        """True if the tab is still open and hasn't crashed."""
        if self.page:
            try:
                return not self.page.is_closed()
            except Exception:
                pass
        return False


@dataclass
class QrAuthToken:
    """Validated QR code auth token from government app.

    Attributes:
        token: The raw auth token string.
        challenge_id: The challenge this token answered.
        expires_at: Unix timestamp when the token expires.
        metadata: Optional extra claims from the government app.
    """
    token: str = ""
    challenge_id: str = ""
    expires_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() > self.expires_at


@dataclass
class SessionState:
    """Snapshot of the session's current state."""
    authenticated: bool = False
    on_vm_list: bool = False
    current_url: str = ""
    vm_count: int = 0
    backend: str = ""
    mfa_pending: bool = False
    reauth_count: int = 0
    last_reauth_time: float = 0.0
    uptime_seconds: float = 0.0
