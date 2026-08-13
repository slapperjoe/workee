#!/usr/bin/env python3
"""CLI entry point for the Azure Chromium Wrapper.

Usage:
    python -m azure_wrapper --backend avd --list
    python -m azure_wrapper --backend avd --connect "VM Name"
    python -m azure_wrapper --backend portal --list
    python -m azure_wrapper --backend portal --connect /subscriptions/.../vm-name
    python -m azure_wrapper --interactive
    python -m azure_wrapper --demo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

from azure_wrapper import AVDSession, AzureConfig, PortalSession

load_dotenv()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

logger = logging.getLogger("azure_wrapper")


async def _cmd_list(config: AzureConfig) -> None:
    """List VMs and exit."""
    session_cls = AVDSession if config.backend == "avd" else PortalSession

    async with session_cls(config) as session:
        # Handle MFA if needed
        if session.mfa_pending:
            code = input("Enter MFA code: ")
            session.provide_mfa_code(code)
            # Wait a moment for MFA to resolve
            await asyncio.sleep(5)

        vms = await session.get_vms()
        print(f"\n=== {'AVD' if config.backend == 'avd' else 'Azure Portal'} — "
              f"{len(vms)} resource(s) ===\n")
        for i, vm in enumerate(vms, 1):
            status = ""
            if vm.power_state:
                status = f" [{vm.power_state}]"
            if vm.location:
                status += f" ({vm.location})"
            print(f"  [{i}] {vm.name}{status}")
            if vm.workspace:
                print(f"      Workspace: {vm.workspace}")
            if vm.id:
                print(f"      ID: {vm.id}")
            print()
        if not vms:
            print("No resources found.\n")


async def _cmd_connect(config: AzureConfig, vm_id: str) -> None:
    """Connect to a VM and keep the session alive."""
    session_cls = AVDSession if config.backend == "avd" else PortalSession
    session = session_cls(config)
    await session.start()

    # Handle MFA if needed
    if session.mfa_pending:
        code = input("Enter MFA code: ")
        session.provide_mfa_code(code)
        await asyncio.sleep(5)

    try:
        print(f"\nConnecting to {vm_id}...")
        conn = await session.connect(vm_id)
        print(f"Connected via {conn.method}")
        print("Press Ctrl+C to disconnect.\n")

        # Keep alive
        keep_alive = asyncio.create_task(session.run_loop())
        try:
            await asyncio.gather(
                keep_alive,
                conn.wait_for_disconnect(),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
    except Exception as exc:
        logger.error("Connection failed: %s", exc)
        sys.exit(1)
    finally:
        await session.stop()


async def _cmd_interactive(config: AzureConfig) -> None:
    """Interactive session with commands."""
    session_cls = AVDSession if config.backend == "avd" else PortalSession
    session = session_cls(config)
    await session.start()

    if session.mfa_pending:
        code = input("Enter MFA code: ")
        session.provide_mfa_code(code)
        await asyncio.sleep(5)

    loop = asyncio.get_event_loop()
    print("\nInteractive mode — type commands (or 'help'):", flush=True)
    print(f"  Backend: {config.backend}", flush=True)

    running = True

    # Keep-alive task
    async def _keep_alive():
        await session.run_loop()
        nonlocal running
        running = False

    keep_task = asyncio.create_task(_keep_alive())

    while running:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            break

        cmd = line.strip().lower()

        if cmd in ("help", "h", "?"):
            print(
                "\nCommands:\n"
                "  list / ls        — list available VMs\n"
                "  connect <name>   — connect to a VM\n"
                "  status / st      — show session status\n"
                "  quit / exit / q  — shutdown",
                flush=True,
            )
        elif cmd in ("list", "ls"):
            vms = await session.get_vms()
            if vms:
                print(f"\n{len(vms)} resource(s):\n", flush=True)
                for i, vm in enumerate(vms, 1):
                    info = f"  [{i}] {vm.name}"
                    if vm.power_state:
                        info += f" [{vm.power_state}]"
                    print(info, flush=True)
                    if vm.id:
                        print(f"      ID: {vm.id}", flush=True)
                    print(flush=True)
            else:
                print("No resources found.", flush=True)
        elif cmd.startswith("connect ") or cmd.startswith("vm "):
            vm_name = line.strip().split(" ", 1)[1] if " " in line.strip() else ""
            if vm_name:
                try:
                    conn = await session.connect(vm_name)
                    print(f"Connected via {conn.method}", flush=True)
                except Exception as exc:
                    print(f"Failed: {exc}", flush=True)
            else:
                print("Usage: connect <vm-name>", flush=True)
        elif cmd in ("status", "st"):
            state = await session.get_state()
            print(
                f"\nSession Status:\n"
                f"  Authenticated: {state.authenticated}\n"
                f"  On VM List:    {state.on_vm_list}\n"
                f"  URL:           {state.current_url[:120]}\n"
                f"  VM Count:      {state.vm_count}\n"
                f"  Backend:       {state.backend}\n"
                f"  MFA Pending:   {state.mfa_pending}\n"
                f"  Re-auths:      {state.reauth_count}\n"
                f"  Uptime:        {state.uptime_seconds:.0f}s",
                flush=True,
            )
        elif cmd in ("quit", "exit", "q"):
            break
        else:
            print(f"Unknown command: {cmd!r} (type 'help')", flush=True)

    keep_task.cancel()
    try:
        await keep_task
    except asyncio.CancelledError:
        pass
    await session.stop()


async def _cmd_demo() -> None:
    """Run a demo with mocked/offline flows."""
    print("=" * 60)
    print("Azure Chromium Wrapper — Demo")
    print("=" * 60)
    print()

    config = AzureConfig(
        email="demo@example.com",
        password="demo",
        backend="avd",
        mfa_method="auto",
    )

    print("1. Configuration loaded:", config)
    print()
    print("2. Session lifecycle (mock):")
    print("   - AVDSession(config) → creates AuthManager, MfaManager, SessionMonitor")
    print("   - await session.start() → launches Chromium (persistent context)")
    print("   - Navigates to windows.microsoft.cloud")
    print("   - Detects login redirect → fills email/password")
    print("   - MFA: signal/wait pattern (asyncio.Event)")
    print()
    print("3. VM listing:")
    session = AVDSession(config)
    assert session.mfa.mfa_event is not None
    assert session.monitor is not None
    assert session.auth is not None
    print("   - get_vms() → feed discovery API (rdweb.wvd.microsoft.com)")
    print("   - Fallback: DOM scraping with 7 selector strategies")
    print()
    print("4. VM connection:")
    print("   - connect(vm_id) → opens new browser tab")
    print("   - Detects new tab via context.on('page')")
    print("   - Returns VmConnection with page handle")
    print()
    print("5. Session monitor:")
    print("   - Heartbeat: checks login redirect every 15s")
    print("   - Re-auth: triggers full auth flow on expiry")
    print("   - Counters: reauth_count, last_reauth_time")
    print()
    print("=" * 60)
    print("Module structure:")
    print("=" * 60)

    import azure_wrapper
    import os as _os
    pkg_dir = _os.path.dirname(azure_wrapper.__file__)
    for f in sorted(_os.listdir(pkg_dir)):
        if f.endswith(".py") and not f.startswith("__pycache__"):
            path = _os.path.join(pkg_dir, f)
            lines = len(open(path).readlines())
            print(f"  {f:<20} {lines:>5} lines")
    print()
    print("API surface:")
    print("  from azure_wrapper import AzureConfig, AVDSession, PortalSession")
    print("  from azure_wrapper import VmInfo, VmConnection, SessionState")
    print()
    print("See README.md for real authentication setup instructions.")


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Azure Chromium Wrapper — headless browser VM access",
    )
    parser.add_argument(
        "--backend", "-b",
        choices=["avd", "portal"],
        default="avd",
        help="Backend to use (default: avd)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available VMs and exit",
    )
    parser.add_argument(
        "--connect", "-c",
        help="Connect to a VM by name or resource ID",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive command mode",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a demo with mocked flows (no browser)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show browser window",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.demo:
        await _cmd_demo()
        return

    # Load config from environment
    config = AzureConfig.from_env()
    config.backend = args.backend
    if args.headed:
        config.headed = True
    if args.verbose:
        config.log_level = "DEBUG"

    _setup_logging(config.log_level)

    if not config.email or not config.password:
        print(
            "ERROR: Azure credentials not set.\n"
            "Set AZURE_EMAIL and AZURE_PASSWORD environment variables,\n"
            "or create a .env file from config.example.env.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info("Starting Azure session (backend: %s)", config.backend)

    if args.list:
        await _cmd_list(config)
    elif args.connect:
        await _cmd_connect(config, args.connect)
    elif args.interactive:
        await _cmd_interactive(config)
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
