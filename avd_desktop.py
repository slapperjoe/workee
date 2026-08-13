#!/usr/bin/env python3
"""
avd_desktop.py — Native desktop app for browsing and connecting to AVD VMs.

Opens a native window (pywebview) showing your Cloud PCs / VMs as tiles.
Click any VM to open its remote desktop in your default browser.

The app keeps a Playwright session alive in the background for authentication
and VM discovery. Session expiration triggers automatic re-authentication.

Usage:
    python avd_desktop.py
    python avd_desktop.py --headed    # show Playwright browser during auth

Environment variables (see config.example.env):
    AVD_EMAIL, AVD_PASSWORD, AVD_MFA_METHOD, AVD_TOTP_SECRET, etc.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing avd_session (which also loads it)
_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

logger = logging.getLogger("avd_desktop")

# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AVD VM Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --text-muted: #8b949e; --accent: #58a6ff;
    --accent-hover: #79c0ff; --success: #3fb950; --warning: #d29922;
    --error: #f85149; --radius: 8px;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    background:var(--bg);color:var(--text);min-height:100vh;line-height:1.5;
    -webkit-app-region:no-drag;
  }
  header{
    background:var(--surface);border-bottom:1px solid var(--border);
    padding:12px 24px;display:flex;align-items:center;
    justify-content:space-between;flex-wrap:wrap;gap:8px;
    -webkit-app-region:drag;
  }
  header h1{font-size:18px;font-weight:600;color:var(--accent);-webkit-app-region:no-drag}
  .status-bar{display:flex;align-items:center;gap:16px;font-size:13px;color:var(--text-muted);-webkit-app-region:no-drag}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
  .dot.online{background:var(--success)}.dot.offline{background:var(--error)}.dot.warning{background:var(--warning)}
  .dot.connecting{background:var(--warning);animation:pulse 1s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  main{max-width:1200px;margin:0 auto;padding:24px}
  .section-title{font-size:14px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);margin-bottom:12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-bottom:32px}
  .tile{
    background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:20px;cursor:pointer;transition:border-color .15s,box-shadow .15s;
  }
  .tile:hover{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .tile:active{transform:scale(.98)}
  .tile h3{font-size:16px;font-weight:600;margin-bottom:4px}
  .tile .meta{font-size:12px;color:var(--text-muted);margin-bottom:14px}
  .tile .badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:12px;font-weight:600;margin-right:8px}
  .badge.running{background:rgba(63,185,80,.15);color:var(--success)}
  .badge.stopped{background:rgba(139,148,158,.15);color:var(--text-muted)}
  .badge.cloudpc{background:rgba(88,166,255,.15);color:var(--accent)}
  .tile .btn{display:block;width:100%;padding:10px;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none;transition:opacity .15s}
  .tile .btn.connect{background:var(--accent);color:#fff}
  .tile .btn.connect:hover{opacity:.85}
  .tile .btn.no-link{background:var(--surface);border:1px solid var(--border);color:var(--text-muted);cursor:default}
  .footer-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
  .footer-bar button{padding:6px 14px;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer;font-size:13px}
  .footer-bar button:hover{border-color:var(--accent)}
  .empty-state{text-align:center;padding:48px 24px;color:var(--text-muted)}
  .empty-state .icon{font-size:48px;margin-bottom:12px}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;border-radius:var(--radius);font-size:14px;font-weight:600;animation:fadeIn .2s;z-index:100}
  .toast.success{background:var(--success);color:#000}
  .toast.error{background:var(--error);color:#fff}
  @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  .footer{text-align:center;padding:12px;color:var(--text-muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>AVD VM Dashboard</h1>
  <div class="status-bar" id="statusBar">
    <span id="stateIndicator"><span class="dot connecting"></span> Connecting...</span>
  </div>
</header>
<main>
  <div class="footer-bar">
    <span class="section-title" style="margin-bottom:0">Virtual Machines</span>
    <button onclick="refreshVms()" id="refreshBtn">&#x21bb; Refresh</button>
  </div>
  <div class="grid" id="vmGrid">
    <div class="empty-state"><div class="icon">&#x1F5B4;</div><p>Loading VMs...</p></div>
  </div>
</main>
<div class="footer">Press Ctrl+Q or Cmd+Q to quit</div>
<script>
// --- JS-Python bridge ---
// pywebview injects window.pywebview.api with our backend methods.
// Fallback: try direct pywebview.api
const api = window.pywebview ? window.pywebview.api : null;

function el(id){return document.getElementById(id)}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

let pollTimer = null;

// --- Backend calls ---
async function callApi(method, ...args) {
  if (!api || !api[method]) {
    throw new Error('Backend not available');
  }
  // pywebview JS API returns results via callback pattern or direct return
  // In newer pywebview, they return Promises
  if (api[method].constructor.name === 'AsyncFunction' || api[method].toString().includes('async')) {
    return await api[method](...args);
  }
  return api[method](...args);
}

// --- State polling ---
async function refreshState() {
  if (!api) return;
  try {
    const state = await callApi('get_state');
    const ind = el('stateIndicator');
    if (state.authenticated) {
      ind.innerHTML = '<span class="dot online"></span> Authenticated · '
        + state.vm_count + ' VMs · uptime ' + fmtUptime(state.uptime_seconds);
    } else {
      ind.innerHTML = '<span class="dot connecting"></span> Connecting...';
    }
  } catch(e) {
    el('stateIndicator').innerHTML = '<span class="dot offline"></span> Backend unavailable';
  }
}

function fmtUptime(s) {
  if (!s) return '--';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h>0 ? h+'h '+m+'m' : m+'m';
}

// --- VM listing ---
async function refreshVms() {
  if (!api) {
    document.getElementById('vmGrid').innerHTML =
      '<div class="empty-state"><div class="icon">&#x26A0;</div><p>Backend not ready — waiting for session...</p></div>';
    return;
  }
  const grid = el('vmGrid'), btn = el('refreshBtn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Loading';
  try {
    const vms = await callApi('list_vms');
    renderVms(vms || []);
  } catch(e) {
    grid.innerHTML = '<div class="empty-state"><div class="icon" style="color:var(--error)">&#x26A0;</div><p>'+esc(e.message||'Error loading VMs')+'</p></div>';
  } finally {
    btn.disabled = false; btn.innerHTML = '&#x21bb; Refresh';
  }
}

function renderVms(vms) {
  const grid = el('vmGrid');
  if (!vms || vms.length === 0) {
    grid.innerHTML = '<div class="empty-state"><div class="icon">&#x1F50D;</div><p>No VMs found. Check your Azure subscription.</p></div>';
    return;
  }
  grid.innerHTML = vms.map(vm => {
    const hasLink = !!vm.resource_id;
    const status = vm.status || 'Unknown';
    const isRunning = status.toLowerCase().includes('running');
    const badgeCls = isRunning ? 'running' : (vm.kind==='CloudPC' ? 'cloudpc' : 'stopped');
    return '<div class="tile" onclick="connectVm(\''+escAttr(vm.name)+'\')">'
      + '<h3>'+esc(vm.name)+'</h3>'
      + '<div class="meta"><span class="badge '+badgeCls+'">'+esc(status)+'</span>'
      + (vm.kind ? '<span>'+esc(vm.kind)+'</span>' : '')
      + (vm.specs ? '<span> &middot; '+esc(vm.specs)+'</span>' : '')
      + '</div>'
      + '<button class="btn connect" onclick="event.stopPropagation();connectVm(\''+escAttr(vm.name)+'\')">Connect &rarr;</button>'
      + '</div>';
  }).join('');
}

// --- VM connection ---
async function connectVm(vmName) {
  try {
    const result = await callApi('connect_vm', vmName);
    showToast('Connected to ' + vmName, 'success');
  } catch(e) {
    showToast('Failed: ' + (e.message || 'Connection error'), 'error');
  }
}

// --- Toast ---
function showToast(msg, cls) {
  const toast = document.createElement('div');
  toast.className = 'toast ' + cls;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// --- Utils ---
function escAttr(s) {
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// --- Init ---
function init() {
  // Poll for backend readiness, then load data
  let attempts = 0;
  const waitForBackend = setInterval(() => {
    if (api) {
      clearInterval(waitForBackend);
      refreshState();
      refreshVms();
      pollTimer = setInterval(refreshState, 10000);
    } else if (++attempts > 60) {
      clearInterval(waitForBackend);
      el('stateIndicator').innerHTML = '<span class="dot offline"></span> Backend not available';
    }
  }, 500);
}

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Desktop application
# ---------------------------------------------------------------------------

class AVDBackend:
    """Bridge between pywebview frontend and Playwright AVD session.

    Runs the AVDSessionManager in a background asyncio loop and exposes
    methods that the JavaScript frontend can call via pywebview's JS API.
    """

    def __init__(self, headed: bool = False):
        self.headed = headed
        self._manager = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: str | None = None

    # ---- Public API (exposed to JS via pywebview) ----

    def get_state(self) -> dict:
        """Return current session state."""
        if not self._manager:
            return {"authenticated": False, "vm_count": 0, "uptime_seconds": 0}
        try:
            state = self._run_coro(self._manager.get_state())
            return {
                "authenticated": state.authenticated,
                "on_dashboard": state.on_dashboard,
                "vm_count": state.vm_count,
                "reauth_count": state.reauth_count,
                "uptime_seconds": state.uptime_seconds,
                "current_url": state.current_url[:120],
            }
        except Exception as exc:
            return {"authenticated": False, "vm_count": 0, "uptime_seconds": 0, "error": str(exc)}

    def list_vms(self) -> list[dict]:
        """Return available VMs with metadata."""
        if not self._manager:
            return []
        try:
            vms = self._run_coro(self._manager.list_vms())
            return [
                {
                    "name": vm.get("name", ""),
                    "kind": vm.get("kind", ""),
                    "workspace": vm.get("workspace", ""),
                    "resource_id": vm.get("resource_id", ""),
                    "status": vm.get("status", "Unknown"),
                    "specs": vm.get("specs", ""),
                }
                for vm in vms
            ]
        except Exception as exc:
            logger.exception("list_vms failed")
            return [{"name": f"Error: {exc}", "kind": "", "status": "Error", "resource_id": ""}]

    def connect_vm(self, vm_name: str) -> dict:
        """Connect to a VM by name. Opens webclient in default browser if resource_id found."""
        if not self._manager:
            return {"status": "error", "message": "Session not started"}

        try:
            # First get the VM list to find the resource_id
            vms = self._run_coro(self._manager.list_vms())
            resource_id = ""
            for vm in vms:
                if vm_name.lower() in vm.get("name", "").lower():
                    resource_id = vm.get("resource_id", "")
                    break

            # Open in system browser if we have a resource ID
            if resource_id:
                url = f"https://windows.cloud.microsoft/webclient/index.html?resourceId={resource_id}"
                logger.info("Opening webclient in browser: %s", url)
                webbrowser.open(url, new=2)  # new=2 = new tab
                return {"status": "connected", "vm_name": vm_name, "method": "webclient"}

            # Fall back to Playwright-based connection (opens in Playwright browser)
            logger.info("No resource_id — connecting via Playwright")
            ok = self._run_coro(self._manager.connect_to_vm(vm_name))
            if ok:
                return {"status": "connected", "vm_name": vm_name, "method": "playwright"}
            return {"status": "error", "message": f"Could not find VM: {vm_name}"}

        except Exception as exc:
            logger.exception("connect_vm failed")
            return {"status": "error", "message": str(exc)}

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the backend in a background thread."""
        thread = threading.Thread(target=self._run_session, daemon=True)
        thread.start()

    def stop(self) -> None:
        """Shut down the backend."""
        if self._loop and self._manager:
            asyncio.run_coroutine_threadsafe(self._manager.stop(), self._loop)

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until the session is ready or timeout."""
        return self._ready.wait(timeout)

    @property
    def error(self) -> str | None:
        return self._error

    # ---- Internal ----

    def _run_session(self) -> None:
        """Run the AVD session in a dedicated asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_session())
        except Exception as exc:
            logger.exception("Session thread crashed")
            self._error = str(exc)
        finally:
            self._loop.close()

    async def _start_session(self) -> None:
        """Create AVDSessionManager, authenticate, and start keep-alive."""
        from avd_session import AVDSessionManager, _get_config

        config = _get_config()

        self._manager = AVDSessionManager(
            email=config["email"],
            password=config["password"],
            mfa_method=config["mfa_method"],
            totp_secret=config["totp_secret"],
            mfa_timeout=config["mfa_timeout"],
            mfa_post_submit_timeout=config["mfa_post_submit_timeout"],
            storage_state_path=config["storage_state_path"],
            poll_interval=config["poll_interval"],
            reauth_timeout=config["reauth_timeout"],
            headed=self.headed,
        )

        logger.info("Starting AVD session...")
        ok = await self._manager.start()
        if not ok:
            self._error = "Failed to start AVD session — check credentials"
            logger.error(self._error)
            self._ready.set()
            return

        logger.info("AVD session ready")
        self._ready.set()

        # Run keep-alive loop (blocks until stopped)
        try:
            await self._manager.run()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Session loop error")

    def _run_coro(self, coro):
        """Run a coroutine on the background loop, blocking the calling thread."""
        if self._loop is None:
            raise RuntimeError("Event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AVD VM Desktop Dashboard")
    parser.add_argument("--headed", action="store_true", help="Show Playwright browser window")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.getenv("AVD_EMAIL") or not os.getenv("AVD_PASSWORD"):
        print("ERROR: AVD_EMAIL and AVD_PASSWORD must be set in .env file.", file=sys.stderr)
        sys.exit(1)

    # Start AVD backend in background thread
    backend = AVDBackend(headed=args.headed)
    backend.start()

    print("Starting AVD session (authenticating in background)...", flush=True)

    # Wait for session to be ready before showing the window
    if not backend.wait_ready(timeout=60.0):
        print(f"ERROR: Session start failed: {backend.error or 'timeout'}", file=sys.stderr)
        backend.stop()
        sys.exit(1)

    print("AVD session ready — opening dashboard window.", flush=True)

    # Launch native window with pywebview
    import webview

    window = webview.create_window(
        title="AVD VM Dashboard",
        html=DASHBOARD_HTML,
        js_api=backend,
        width=1100,
        height=750,
        min_size=(700, 400),
        resizable=True,
        fullscreen=False,
    )

    webview.start(debug=args.verbose)

    # Cleanup on window close
    print("Shutting down AVD session...", flush=True)
    backend.stop()


if __name__ == "__main__":
    main()
