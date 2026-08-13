#!/usr/bin/env python3
"""VM Launch Dashboard — web UI to browse and connect to Azure VMs.

Runs a Flask web server alongside a persistent Azure session.
Shows available VMs as tiles; one click opens a new Chromium tab
with the VM's remote desktop (RDP via AVD web client or Bastion).

Usage:
    python dashboard_server.py              # default http://localhost:8080
    python dashboard_server.py --port 9000  # custom port
    python dashboard_server.py --headed     # show browser during auth

Stored VM config is read from dashboard/config.json (created on first run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from azure_wrapper import AVDSession, AzureConfig, PortalSession, VmInfo, VmConnection
from azure_wrapper.resolution import parse as parse_resolution, PRESETS, get_resolution, FALLBACK

logger = logging.getLogger("dashboard")

DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
CONFIG_PATH = DASHBOARD_DIR / "config.json"
STATIC_DIR = DASHBOARD_DIR / "static"
DEFAULT_PORT = 8080

# Ensure static dir exists
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)


def _json_serial(obj: Any) -> Any:
    """JSON serializer for objects not natively serializable."""
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


@app.route("/")
def index():
    """Serve the dashboard HTML."""
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


# ---------------------------------------------------------------------------
# Session state (shared between Flask thread and async lifecycle)
# ---------------------------------------------------------------------------

_session: AVDSession | PortalSession | None = None
_session_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None

# Client-reported viewport (set by dashboard frontend on load)
_client_viewport: dict[str, int] | None = None


def _run_async(coro):
    """Schedule a coroutine on the main loop from a Flask thread."""
    if _loop is None:
        raise RuntimeError("Event loop not available")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


@app.route("/api/state")
def api_state():
    """Return current session state."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"status": "disconnected", "message": "Session not started"})
        try:
            state = _run_async(_session.get_state())
            return jsonify({
                "status": "connected",
                "backend": state.backend,
                "authenticated": state.authenticated,
                "on_vm_list": state.on_vm_list,
                "vm_count": state.vm_count,
                "mfa_pending": state.mfa_pending,
                "reauth_count": state.reauth_count,
                "uptime_seconds": state.uptime_seconds,
                "current_url": state.current_url[:120],
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/vms")
def api_vms():
    """List available VMs."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"error": "Session not started"}), 503
    try:
        vms = _run_async(_session.get_vms())
        return jsonify({
            "count": len(vms),
            "vms": [
                {
                    "id": vm.id,
                    "name": vm.name,
                    "kind": vm.kind,
                    "workspace": vm.workspace,
                    "location": vm.location,
                    "power_state": vm.power_state,
                }
                for vm in vms
            ],
        })
    except Exception as exc:
        logger.exception("Failed to list VMs")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Connect to a VM by id or name. Opens a new browser tab."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"error": "Session not started"}), 503

    body = request.get_json(silent=True) or {}
    vm_id = body.get("vm_id", "").strip()
    if not vm_id:
        return jsonify({"error": "Missing vm_id"}), 400

    try:
        conn = _run_async(_session.connect(vm_id))
        return jsonify({
            "status": "connected",
            "vm_id": conn.vm_id,
            "vm_name": conn.vm_name,
            "method": conn.method,
            "backend": conn.backend,
        })
    except Exception as exc:
        logger.exception("Connection failed for VM %s", vm_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/start", methods=["POST"])
def api_start():
    """Start a stopped VM (Portal backend only)."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"error": "Session not started"}), 503

    body = request.get_json(silent=True) or {}
    vm_id = body.get("vm_id", "").strip()
    if not vm_id:
        return jsonify({"error": "Missing vm_id"}), 400

    try:
        # For PortalSession, call start_vm; for AVDSession this is a no-op
        result = _run_async(_session.start_vm(vm_id))
        return jsonify({"status": "started", "vm_id": vm_id, "result": str(result)})
    except AttributeError:
        return jsonify({
            "status": "not_supported",
            "message": f"VM start is not supported for backend '{_session.config.backend}'. "
                       "AVD VMs power on when connected.",
        })
    except Exception as exc:
        logger.exception("Failed to start VM %s", vm_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mfa", methods=["POST"])
def api_mfa():
    """Submit an MFA code to the session."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"error": "Session not started"}), 503

    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"error": "Missing code"}), 400

    try:
        if _session.mfa_pending:
            _session.provide_mfa_code(code)
            return jsonify({"status": "ok"})
        return jsonify({"status": "no_mfa_needed"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mfa", methods=["GET"])
def api_mfa_status():
    """Return MFA state, including the QR login page when one is active."""
    with _session_lock:
        if _session is None:
            return jsonify({"pending": False, "method": None, "qr_login_url": ""})
        return jsonify({
            "pending": _session.mfa_pending,
            "method": _session.config.mfa_method,
            "qr_login_url": _session.qr_login_url,
        })


@app.route("/api/connections")
def api_connections():
    """List active VM connections."""
    global _session
    with _session_lock:
        if _session is None:
            return jsonify({"connections": []})
        conns = _session._connections
        return jsonify({
            "connections": [
                {
                    "vm_id": c.vm_id,
                    "vm_name": c.vm_name,
                    "method": c.method,
                    "is_connected": c.is_connected,
                }
                for c in conns
            ]
        })


@app.route("/api/resolution", methods=["GET", "POST"])
def api_resolution():
    """Get or set the browser viewport resolution.

    GET returns the current resolution (client viewport, configured setting,
    and available presets). POST sets the client-reported viewport size
    from the browser frontend.
    """
    global _client_viewport, _session

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        width = body.get("width", 0)
        height = body.get("height", 0)
        if width > 0 and height > 0:
            _client_viewport = {"width": int(width), "height": int(height)}
            # If session is already running, patch the config
            with _session_lock:
                if _session is not None:
                    _session.config._client_viewport = _client_viewport
                    logger.debug(
                        "Client viewport updated: %dx%d", width, height
                    )
            return jsonify({"status": "ok", "viewport": _client_viewport})
        return jsonify({"error": "Missing width/height"}), 400

    # GET: return current resolution state
    configured = None
    with _session_lock:
        if _session is not None:
            configured = _session.config.resolution

    # Resolve the currently active resolution using the full priority chain
    active = get_resolution(
        configured=configured,
        client_viewport=_client_viewport,
        fallback=FALLBACK,
    )

    return jsonify({
        "client_viewport": _client_viewport,
        "configured": configured,
        "presets": PRESETS,
        "active": active,
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Get or update stored VM configuration."""
    if request.method == "GET":
        if CONFIG_PATH.exists():
            try:
                return jsonify(json.loads(CONFIG_PATH.read_text()))
            except Exception:
                pass
        return jsonify({
            "saved_vms": [],
            "default_backend": "avd",
            "notes": "Add VMs here to pre-populate the dashboard.",
        })

    # POST — update config
    body = request.get_json(silent=True) or {}
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(body, indent=2))
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Long-running background: Azure session lifecycle
# ---------------------------------------------------------------------------

async def _run_azure_session(config: AzureConfig) -> None:
    """Start the Azure session and keep it alive."""
    global _session
    session_cls = AVDSession if config.backend == "avd" else PortalSession

    logger.info("Starting Azure session (backend=%s)...", config.backend)
    session = session_cls(config)

    # Inject client viewport if available
    if _client_viewport:
        config._client_viewport = _client_viewport
        logger.debug(
            "Injecting client viewport: %dx%d",
            _client_viewport["width"], _client_viewport["height"],
        )

    try:
        ok = await session.start()
        if not ok:
            logger.error("Session start failed")
            return

        # Handle MFA if needed
        if session.mfa_pending:
            logger.warning(
                "MFA is required! Enter code via the dashboard or restart with --headed."
            )
            # Mark as pending — dashboard will show MFA prompt
        else:
            logger.info("Session authenticated")

        with _session_lock:
            _session = session

        logger.info("Session running — entering keep-alive loop")
        await session.run_loop()

    except asyncio.CancelledError:
        logger.info("Session cancelled")
    except Exception as exc:
        logger.exception("Session error: %s", exc)
    finally:
        with _session_lock:
            _session = None
        await session.stop()


def _flask_thread(port: int):
    """Run Flask in a daemon thread."""
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def _main_async(args) -> None:
    """Main async entry point."""
    global _loop
    _loop = asyncio.get_running_loop()

    # Load config
    config = AzureConfig.from_env()
    # Dashboard preferences are persisted separately from credentials. Apply
    # them on startup so the setup panel is useful across restarts.
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            if saved.get("default_backend") in ("avd", "portal"):
                config.backend = saved["default_backend"]
            if saved.get("resolution"):
                config.resolution = saved["resolution"]
            if saved.get("mfa_method") in ("qr", "auto", "manual", "totp"):
                config.mfa_method = saved["mfa_method"]
        except (OSError, ValueError):
            pass
    # An explicit CLI backend remains authoritative.
    if args.backend != "avd":
        config.backend = args.backend
    if args.headed:
        config.headed = True
    if args.verbose:
        config.log_level = "DEBUG"

    if not config.email or not config.password:
        print(
            "ERROR: Azure credentials not set.\n"
            "Set AZURE_EMAIL and AZURE_PASSWORD environment variables,\n"
            "or create a .env file from config.example.env.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure config.json exists
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "saved_vms": [],
            "default_backend": config.backend,
            "notes": "Add VMs with names/IDs you frequently connect to.",
        }, indent=2))

    # Start Flask in background thread
    port = args.port
    flask = threading.Thread(target=_flask_thread, args=(port,), daemon=True)
    flask.start()

    print(f"\n  Dashboard: http://localhost:{port}\n", flush=True)

    # Start Azure session (blocking)
    await _run_azure_session(config)


def main():
    parser = argparse.ArgumentParser(
        description="VM Launch Dashboard",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--backend", "-b",
        choices=["avd", "portal"],
        default="avd",
        help="Azure backend",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show browser window (needed for initial MFA)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)


if __name__ == "__main__":
    main()
