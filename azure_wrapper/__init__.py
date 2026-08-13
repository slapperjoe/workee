"""Azure Chromium Wrapper — headless browser access to Azure VM sessions.

Provides a clean async Python API for logging into Azure, listing VMs
(AVD and Portal backends), and opening remote sessions (RDP, Bastion,
Serial Console).

Example usage:

    from azure_wrapper import AzureConfig, AVDSession

    config = AzureConfig.from_env()
    async with AVDSession(config) as session:
        vms = await session.get_vms()
        for vm in vms:
            print(f"  {vm.name}")
        conn = await session.connect(vms[0].id)
        print(f"Connected to {conn.vm_name}")
        await conn.wait_for_disconnect()

MFA signal/wait pattern:

    session = AVDSession(config)
    task = asyncio.create_task(session.start())

    # Wait for MFA or completion
    done, pending = await asyncio.wait(
        [task, asyncio.create_task(session.mfa_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if session.mfa_pending:
        code = input("Enter MFA code: ")
        session.provide_mfa_code(code)
        await task
"""

from azure_wrapper.auth import AuthPhase, AuthManager
from azure_wrapper.config import AzureConfig
from azure_wrapper.resolution import get_resolution, parse as parse_resolution, PRESETS as RESOLUTION_PRESETS
from azure_wrapper.session import AzureSession
from azure_wrapper.avd import AVDSession
from azure_wrapper.portal import PortalSession
from azure_wrapper.qr_auth import (
    QrAuthChallenge,
    QrAuthConfig,
    QrAuthManager,
    QrAuthResult,
    QrAuthServer,
    run_qr_auth_flow,
)
from azure_wrapper.types import (
    Credentials,
    QrAuthToken,
    SessionState,
    VmConnection,
    VmInfo,
)

__all__ = [
    "AuthPhase",
    "AuthManager",
    "AzureConfig",
    "AzureSession",
    "AVDSession",
    "PortalSession",
    "get_resolution",
    "parse_resolution",
    "RESOLUTION_PRESETS",
    "QrAuthChallenge",
    "QrAuthConfig",
    "QrAuthManager",
    "QrAuthResult",
    "QrAuthServer",
    "QrAuthToken",
    "run_qr_auth_flow",
    "Credentials",
    "SessionState",
    "VmConnection",
    "VmInfo",
]
