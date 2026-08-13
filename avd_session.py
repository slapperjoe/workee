#!/usr/bin/env python3
"""
avd_session.py — Persistent AVD browser session with auto-reauthentication.

Keeps a long-running Chromium session on the AVD dashboard. Detects session
expiration (redirect to login), automatically re-authenticates using stored
credentials + MFA, and restores the dashboard. Supports launching VM remote
sessions on demand via CLI or programmatic API.

Usage:
    # Start persistent session (keeps dashboard alive, detects expiry)
    python avd_session.py

    # Connect to a specific VM, then keep the session alive
    python avd_session.py --vm "My Desktop"

    # Connect to a VM and exit after the connection tab opens
    python avd_session.py --vm "My Desktop" --no-keep

    # List available VMs and exit
    python avd_session.py --list

    # Start a VM by name with an interactive browser window
    python avd_session.py --start "My Desktop" --headed

    # Start a VM by list index (from --list output)
    python avd_session.py --start 1 --headed

    # Pick a VM interactively from a numbered list
    python avd_session.py --start --headed

    # Show browser window
    python avd_session.py --headed

    # Interactive mode — read commands from stdin
    python avd_session.py --interactive

Environment variables (see config.example.env):
    AVD_EMAIL, AVD_PASSWORD       — credentials
    AVD_MFA_METHOD                — "totp", "manual", or "auto"
    AVD_TOTP_SECRET               — TOTP secret (for totp mode)
    AVD_MFA_TIMEOUT               — MFA prompt timeout seconds (default 120)
    AVD_STORAGE_STATE_PATH        — where to save/load auth state
    AVD_SESSION_POLL_INTERVAL     — seconds between session health checks (default 15)
    AVD_SESSION_REAUTH_TIMEOUT    — max seconds for re-auth (default 300)
    AVD_HEADED                    — "true" to show browser window
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from avd_mfa import MfaResult, MfaStatus, handle_mfa, wait_for_mfa_resolution
from azure_wrapper.resolution import get_resolution, PRESETS

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

# Load .env
_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

logger = logging.getLogger("avd_session")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTRY_URL = "https://windows.microsoft.cloud"
DASHBOARD_URL = "https://windows.cloud.microsoft/#/devices"
LOGIN_HOST = "login.microsoftonline.com"
SPA_CLIENT_ID = "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"
FEED_DISCOVERY_API = "https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery"

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

CHROMIUM_ARGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--enable-features=SharedArrayBuffer",
    "--enable-features=CrossOriginOpenerPolicy",
]

# VM card/tile selectors — ordered by specificity.
# Windows App (post-2024) uses Cloud PC cards with Fluent UI v9 classes
# and data-testid attributes. Older AVD resource cards use different patterns.
VM_SELECTORS = [
    # Cloud PC cards (Windows App): data-testid="cloudPC-card-{guid}"
    '[data-testid*="cloudPC-card"]',
    # Fluent UI v9 card containers
    '[class*="fui-Card"]',
    '[class*="fui-CardHeader"]',
    # Legacy AVD resource cards
    '[class*="ResourceCard"]',
    '[class*="resource-card"]',
    # Direct webclient links
    'a[href*="/webclient/"]',
    # Devices page links
    'a[href*="devices"]',
    # General list items with links
    '[role="listitem"] a',
    '[role="link"]',
]

# Selectors for extracting VM metadata from Cloud PC cards
CLOUD_PC_METADATA_SELECTORS = {
    "name": '[data-testid*="cloudPC-card-name"], [class*="fui-Text"]:first-child',
    "status": '[data-testid*="cloudPC-card-status"], [class*="StatusBadge"]',
    "specs": '[data-testid*="cloudPC-card-specs"], [class*="fui-Card"] [class*="Text"][class*="secondary"]',
}

# Session expiration indicators (beyond URL redirect to login)
SESSION_EXPIRED_INDICATORS = [
    'text="Your session has expired"',
    'text="Session expired"',
    'text="Sign in to continue"',
    'text="You have been signed out"',
]

DEFAULT_DASHBOARD_PORT = 8080

# ---------------------------------------------------------------------------
# Embedded dashboard HTML
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
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.5}
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
  header h1{font-size:18px;font-weight:600;color:var(--accent)}
  .status-bar{display:flex;align-items:center;gap:16px;font-size:13px;color:var(--text-muted)}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
  .dot.online{background:var(--success)} .dot.offline{background:var(--error)} .dot.warning{background:var(--warning)}
  main{max-width:1200px;margin:0 auto;padding:24px}
  .section-title{font-size:14px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);margin-bottom:12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-bottom:32px}
  .tile{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;cursor:pointer;transition:border-color .15s,box-shadow .15s}
  .tile:hover{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .tile:active{transform:scale(.98)}
  .tile h3{font-size:16px;font-weight:600;margin-bottom:4px}
  .tile .meta{font-size:12px;color:var(--text-muted);margin-bottom:12px}
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
  .toast.error{background:var(--error);color:#fff}
  @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<header>
  <h1>AVD VM Dashboard</h1>
  <div class="status-bar" id="statusBar">
    <span id="stateIndicator">Connecting...</span>
  </div>
</header>
<main>
  <div class="footer-bar">
    <span class="section-title" style="margin-bottom:0">Virtual Machines</span>
    <button onclick="refresh()" id="refreshBtn">&#x21bb; Refresh</button>
  </div>
  <div class="grid" id="vmGrid">
    <div class="empty-state"><div class="icon">&#x1F5B4;</div><p>Loading VMs...</p></div>
  </div>
</main>
<script>
const API = '';
const WEB_CLIENT = 'https://windows.cloud.microsoft/webclient/index.html';

function el(id){return document.getElementById(id)}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

async function api(path,opts={}){
  const r=await fetch(API+path,{headers:{'Content-Type':'application/json',...opts.headers},...opts});
  if(!r.ok){const e=await r.json().catch(()=>({error:r.statusText}));throw new Error(e.error||'HTTP '+r.status)}
  return r.json()
}

async function refreshState(){
  try{
    const s=await api('/api/state');
    const ind=el('stateIndicator');
    let dot='<span class="dot offline"></span>';
    if(s.status==='connected'&&s.authenticated){
      dot='<span class="dot online"></span>';
      ind.innerHTML=dot+` Authenticated · ${s.vm_count} VMs · uptime ${fmtUptime(s.uptime_seconds)}`;
    }else if(s.status==='connecting'){
      dot='<span class="dot warning"></span>';
      ind.innerHTML=dot+' '+s.message;
    }else{
      ind.innerHTML=dot+' '+s.message;
    }
  }catch(e){
    el('stateIndicator').innerHTML='<span class="dot offline"></span> Server unreachable';
  }
}

function fmtUptime(s){if(!s)return'--';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h>0?h+'h '+m+'m':m+'m'}

async function refresh(){
  const grid=el('vmGrid'),btn=el('refreshBtn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Loading';
  try{const d=await api('/api/vms');render(d.vms||[])}
  catch(e){grid.innerHTML='<div class="empty-state"><div class="icon" style="color:var(--error)">&#x26A0;</div><p>'+esc(e.message)+'</p></div>'}
  finally{btn.disabled=false;btn.innerHTML='&#x21bb; Refresh'}
}

function render(vms){
  const grid=el('vmGrid');
  if(!vms||vms.length===0){grid.innerHTML='<div class="empty-state"><div class="icon">&#x1F50D;</div><p>No VMs found.</p></div>';return}
  grid.innerHTML=vms.map(vm=>{
    const hasLink=!!vm.resource_id;
    const status=vm.status||'Unknown';
    const isRunning=status.toLowerCase().includes('running');
    const badgeCls=isRunning?'running':(vm.kind==='CloudPC'?'cloudpc':'stopped');
    const url=hasLink?WEB_CLIENT+'?resourceId='+encodeURIComponent(vm.resource_id):'#';
    const target=hasLink?' target="_blank" rel="noopener"':'';
    return '<div class="tile"><h3>'+esc(vm.name)+'</h3>'
      +'<div class="meta"><span class="badge '+badgeCls+'">'+esc(status)+'</span>'
      +(vm.kind?'<span>'+esc(vm.kind)+'</span>':'')
      +(vm.specs?'<span> · '+esc(vm.specs)+'</span>':'')
      +'</div>'
      +(hasLink
        ?'<a class="btn connect" href="'+url+'"'+target+'>Connect &rarr;</a>'
        :'<span class="btn no-link">No resource link</span>')
      +'</div>';
  }).join('')
}

function init(){refreshState();refresh();setInterval(refreshState,10000)}
init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Flask application (built lazily when dashboard mode activates)
# ---------------------------------------------------------------------------

_dashboard_app: Flask | None = None
_dashboard_session: "AVDSessionManager | None" = None
_dashboard_loop: asyncio.AbstractEventLoop | None = None


def _get_dashboard_app() -> Flask:
    """Create (or return cached) the Flask dashboard application."""
    global _dashboard_app
    if _dashboard_app is not None:
        return _dashboard_app

    _dashboard_app = Flask(__name__)

    @_dashboard_app.route("/")
    def _index():
        return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    @_dashboard_app.route("/api/state")
    def _api_state():
        if _dashboard_session is None:
            return jsonify({"status": "disconnected", "message": "Session not started"})
        try:
            state = _run_async_on_dashboard(_dashboard_session.get_state())
            return jsonify({
                "status": "connected" if state.authenticated else "connecting",
                "authenticated": state.authenticated,
                "on_dashboard": state.on_dashboard,
                "vm_count": state.vm_count,
                "reauth_count": state.reauth_count,
                "uptime_seconds": state.uptime_seconds,
                "current_url": state.current_url[:120],
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @_dashboard_app.route("/api/vms")
    def _api_vms():
        if _dashboard_session is None:
            return jsonify({"error": "Session not started"}), 503
        try:
            vms = _run_async_on_dashboard(_dashboard_session.list_vms())
            return jsonify({
                "count": len(vms),
                "vms": [
                    {
                        "name": vm.get("name", ""),
                        "kind": vm.get("kind", ""),
                        "workspace": vm.get("workspace", ""),
                        "resource_id": vm.get("resource_id", ""),
                        "status": vm.get("status", "Unknown"),
                        "specs": vm.get("specs", ""),
                    }
                    for vm in vms
                ],
            })
        except Exception as exc:
            logger.exception("Failed to list VMs via dashboard API")
            return jsonify({"error": str(exc)}), 500

    return _dashboard_app


def _run_async_on_dashboard(coro):
    """Schedule a coroutine on the main loop from a Flask thread."""
    if _dashboard_loop is None:
        raise RuntimeError("Event loop not available")
    future = asyncio.run_coroutine_threadsafe(coro, _dashboard_loop)
    return future.result(timeout=30)


def _run_flask(port: int) -> None:
    """Run Flask in a daemon thread."""
    app = _get_dashboard_app()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def _run_dashboard_mode(manager: "AVDSessionManager", port: int) -> None:
    """Start both the Flask dashboard and the session keep-alive loop."""
    global _dashboard_session, _dashboard_loop
    _dashboard_session = manager
    _dashboard_loop = asyncio.get_running_loop()

    # Start Flask in background thread
    flask_thread = threading.Thread(
        target=_run_flask, args=(port,), daemon=True
    )
    flask_thread.start()

    print(f"\n  Dashboard: http://localhost:{port}\n", flush=True)
    print(
        "AVD session running. The dashboard shows your VMs.\n"
        "Click a VM to open it in a new browser tab.\n"
        "Press Ctrl+C to exit.\n",
        flush=True,
    )

    # Run the session keep-alive loop
    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_config() -> dict:
    """Read configuration from environment with sensible defaults."""
    email = os.getenv("AVD_EMAIL")
    password = os.getenv("AVD_PASSWORD")

    if not email or not password:
        logger.error(
            "AVD_EMAIL and AVD_PASSWORD must be set in environment or .env file. "
            "Copy config.example.env to .env and fill in your credentials."
        )
        sys.exit(1)

    return {
        "email": email,
        "password": password,
        "mfa_method": os.getenv("AVD_MFA_METHOD", "auto"),
        "totp_secret": os.getenv("AVD_TOTP_SECRET") or None,
        "mfa_timeout": float(os.getenv("AVD_MFA_TIMEOUT", "120")),
        "mfa_post_submit_timeout": float(os.getenv("AVD_MFA_POST_SUBMIT_TIMEOUT", "60")),
        "storage_state_path": os.getenv("AVD_STORAGE_STATE_PATH", "./auth-state.json"),
        "poll_interval": float(os.getenv("AVD_SESSION_POLL_INTERVAL", "15")),
        "reauth_timeout": float(os.getenv("AVD_SESSION_REAUTH_TIMEOUT", "300")),
        "headed": os.getenv("AVD_HEADED", "").lower() in ("1", "true", "yes"),
    }


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """Snapshot of the session manager's current state (for status queries)."""

    authenticated: bool = False
    on_dashboard: bool = False
    current_url: str = ""
    vm_count: int = 0
    reauth_count: int = 0
    last_reauth_time: float = 0.0
    uptime_seconds: float = 0.0


class AVDSessionManager:
    """Manages a persistent AVD browser session with automatic re-auth.

    The session keeps a Chromium tab on the AVD dashboard. It periodically
    checks for session expiration (redirect to Microsoft login) and
    re-authenticates automatically when needed.

    VM connections are opened by clicking the named resource on the
    dashboard, which opens the RDP web client in a new browser tab.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        mfa_method: str = "auto",
        totp_secret: str | None = None,
        mfa_timeout: float = 120.0,
        mfa_post_submit_timeout: float = 60.0,
        storage_state_path: str = "./auth-state.json",
        poll_interval: float = 15.0,
        reauth_timeout: float = 300.0,
        headed: bool = False,
        resolution: str | dict | None = None,
    ):
        self.email = email
        self.password = password
        self.mfa_method = mfa_method
        self.totp_secret = totp_secret
        self.mfa_timeout = mfa_timeout
        self.mfa_post_submit_timeout = mfa_post_submit_timeout
        self.storage_state_path = Path(storage_state_path)
        self.poll_interval = poll_interval
        self.reauth_timeout = reauth_timeout
        self.headed = headed
        self.resolution = resolution

        # Runtime state
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._start_time: float = 0.0
        self._reauth_count: int = 0
        self._last_reauth_time: float = 0.0
        self._vms: list[dict] = []
        self._running: bool = False
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Launch browser, authenticate, and navigate to the dashboard.

        Returns True on success.
        """
        from playwright.async_api import async_playwright

        self._start_time = time.monotonic()
        self._stop_event = asyncio.Event()

        logger.info("Starting Playwright...")
        self._pw = await async_playwright().start()

        self._browser = await self._pw.chromium.launch(
            headless=not self.headed,
            args=CHROMIUM_ARGS,
        )

        # Resolve viewport dynamically with auto-detection support
        viewport = get_resolution(
            configured=self.resolution,
        )

        # Try loading saved state first
        if self.storage_state_path.exists():
            logger.info("Loading saved auth state from %s", self.storage_state_path)
            try:
                self._context = await self._browser.new_context(
                    storage_state=str(self.storage_state_path),
                    user_agent=EDGE_UA,
                    viewport=viewport,
                )
                self._page = await self._context.new_page()
                await self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)

                if await self._check_authenticated():
                    logger.info("Session reuse successful")
                    return True

                logger.info("Saved session expired — performing fresh login")
                await self._context.close()
                self._context = None
                self._page = None
            except Exception as exc:
                logger.warning("Session reuse failed: %s", exc)
                if self._context:
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                    self._context = None
                    self._page = None

        # Fresh login
        if self._context is None:
            self._context = await self._browser.new_context(
                user_agent=EDGE_UA,
                viewport=viewport,
            )
            self._page = await self._context.new_page()

        success = await self._authenticate()
        if not success:
            return False

        # Navigate to dashboard
        await self._navigate_to_dashboard()
        return True

    async def stop(self) -> None:
        """Clean shutdown: save state and close browser."""
        self._running = False
        if self._stop_event:
            self._stop_event.set()

        if self._context and self._page:
            try:
                await self._context.storage_state(path=str(self.storage_state_path))
                logger.info("Saved auth state to %s", self.storage_state_path)
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

        logger.info("Session closed")

    # ------------------------------------------------------------------
    # Main loop — keep the session alive
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the session persistence loop.

        Monitors the dashboard page for session expiration, re-authenticates
        automatically, and keeps the browser alive until interrupted.
        """
        self._running = True
        logger.info("Session persistence loop started (poll interval: %.0fs)", self.poll_interval)

        while self._running:
            try:
                expired = await self._check_session_expired()
                if expired:
                    logger.warning("Session expired — starting re-authentication")
                    await self._reauthenticate()
                    self._reauth_count += 1
                    self._last_reauth_time = time.monotonic()
                    logger.info(
                        "Re-authentication #%d complete — back on dashboard",
                        self._reauth_count,
                    )

                # Wait for poll interval or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval,
                    )
                    break  # stop event was set
                except asyncio.TimeoutError:
                    pass  # poll interval elapsed, check again

            except Exception as exc:
                logger.exception("Error in session loop: %s", exc)
                await asyncio.sleep(5)

        logger.info("Session loop ended")

    # ------------------------------------------------------------------
    # Token extraction
    # ------------------------------------------------------------------

    async def _extract_access_token(self) -> str | None:
        """Extract the WVD access token from the browser's sessionStorage.

        MSAL.js v4 stores tokens in sessionStorage (not localStorage), so
        Playwright's storage_state() won't capture them. We extract the
        token directly via page.evaluate for API calls.

        Returns the access token string, or None if not found.
        """
        if not self._page:
            return None

        try:
            token = await self._page.evaluate("""
                () => {
                    const clientId = '451f2815-40fe-44bb-b8a6-3a2e55cf40c4';
                    const scopeTag = 'accesstoken';

                    // MSAL.js v4 stores tokens in sessionStorage with keys matching:
                    // {homeAccountId}-login.windows.net-accesstoken-{clientId}-{tenant}-{scopes}
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        if (key && key.includes(clientId) && key.includes(scopeTag)) {
                            try {
                                const entry = JSON.parse(sessionStorage.getItem(key));
                                if (entry && entry.secret) {
                                    return entry.secret;
                                }
                            } catch (e) {
                                continue;
                            }
                        }
                    }

                    // Fallback: check localStorage too (older MSAL versions)
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (key && key.includes(clientId) && key.includes(scopeTag)) {
                            try {
                                const entry = JSON.parse(localStorage.getItem(key));
                                if (entry && entry.secret) {
                                    return entry.secret;
                                }
                            } catch (e) {
                                continue;
                            }
                        }
                    }
                    return null;
                }
            """)
            if token:
                logger.debug("Access token extracted from sessionStorage")
            else:
                logger.debug("No access token found in sessionStorage or localStorage")
            return token
        except Exception as exc:
            logger.debug("Token extraction failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # VM operations
    # ------------------------------------------------------------------

    async def list_vms(self) -> list[dict]:
        """Return available VMs from the dashboard.

        Tries the feed discovery API with a Bearer token (extracted from
        sessionStorage since MSAL.js v4 stores tokens there). Falls back
        to DOM scraping if the API is unavailable or the token is missing.
        """
        if not self._page:
            return []

        await self._ensure_on_dashboard()

        # Try feed discovery API with Bearer token
        access_token = await self._extract_access_token()
        if access_token:
            try:
                feed_data = await self._page.evaluate("""
                    async ([apiUrl, token]) => {
                        const resp = await fetch(apiUrl, {
                            headers: {
                                'Authorization': 'Bearer ' + token,
                                'Accept': 'application/json',
                            },
                            credentials: 'include',
                        });
                        if (!resp.ok) {
                            return { _error: true, status: resp.status, body: await resp.text() };
                        }
                        return await resp.json();
                    }
                """, [FEED_DISCOVERY_API, access_token])

                if isinstance(feed_data, dict) and feed_data.get("_error"):
                    logger.debug(
                        "Feed discovery API returned %d: %s",
                        feed_data.get("status"),
                        str(feed_data.get("body", ""))[:200],
                    )
                elif feed_data:
                    vms = self._parse_feed_response(feed_data)
                    if vms:
                        self._vms = vms
                        return vms
            except Exception as exc:
                logger.debug("Feed discovery API call failed: %s", exc)
        else:
            logger.debug("No access token available — skipping feed discovery API")

        # Fall back to DOM scraping
        vms = await self._scrape_vms_from_dom()
        self._vms = vms
        return vms

    async def connect_to_vm(self, vm_name: str) -> bool:
        """Find and click a VM by name to open its remote session.

        The VM click opens the RDP web client in a new browser tab/window.
        Returns True if the VM was found and clicked.
        """
        if not self._page:
            logger.error("No active page — cannot connect to VM")
            return False

        await self._ensure_on_dashboard()
        await asyncio.sleep(3)  # let dashboard render

        logger.info("Looking for VM: %s", vm_name)

        # Strategy 0: If we have the resource_id, navigate directly to webclient
        vms = await self.list_vms()
        for vm in vms:
            if vm_name.lower() in vm.get("name", "").lower():
                resource_id = vm.get("resource_id", "")
                if resource_id:
                    webclient_url = (
                        "https://windows.cloud.microsoft/webclient/index.html"
                        f"?resourceId={resource_id}"
                    )
                    logger.info("Opening webclient: %s", webclient_url)
                    # Open in a new page so the dashboard page stays intact
                    new_page = await self._context.new_page()
                    await new_page.goto(webclient_url, wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                    logger.info("Webclient loaded for VM '%s'", vm_name)
                    return True

        # Strategy 1: Try to find the VM by name text, then click the
        # nearest clickable ancestor or link.
        clicked = await self._click_vm_by_text(vm_name)
        if clicked:
            logger.info("Opened VM '%s' via text match", vm_name)
            return True

        # Strategy 2: Try feed discovery API to get resource IDs, then
        # construct the webclient URL directly and navigate to it.
        vms = await self.list_vms()
        for vm in vms:
            if vm_name.lower() in vm.get("name", "").lower():
                resource_id = vm.get("resource_id", "")
                if resource_id:
                    # Try navigating directly to the webclient
                    webclient_url = (
                        "https://windows.cloud.microsoft/webclient/index.html"
                        f"?resourceId={resource_id}"
                    )
                    logger.info("Navigating to webclient: %s", webclient_url)
                    await self._page.goto(webclient_url, wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                    return True

        logger.warning("Could not find VM '%s' on dashboard", vm_name)
        return False

    async def get_state(self) -> SessionState:
        """Return a snapshot of the session's current state."""
        url = ""
        on_dashboard = False
        authenticated = False

        if self._page:
            try:
                url = self._page.url
                on_dashboard = await self._check_on_dashboard()
                authenticated = await self._check_authenticated()
            except Exception:
                pass

        return SessionState(
            authenticated=authenticated,
            on_dashboard=on_dashboard,
            current_url=url,
            vm_count=len(self._vms),
            reauth_count=self._reauth_count,
            last_reauth_time=self._last_reauth_time,
            uptime_seconds=time.monotonic() - self._start_time,
        )

    # ------------------------------------------------------------------
    # Internal: authentication
    # ------------------------------------------------------------------

    async def _authenticate(self) -> bool:
        """Perform full login: email → password → MFA → dashboard."""
        page = self._page

        # Step 1: Navigate to entry point
        logger.info("Navigating to %s", ENTRY_URL)
        await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        if await self._check_on_dashboard():
            logger.info("Already authenticated (no login redirect)")
            await self._save_state()
            return True

        # Step 2: Wait for Microsoft login page
        if LOGIN_HOST not in page.url:
            logger.info("Waiting for redirect to Microsoft login...")
            try:
                await page.wait_for_url(f"**/{LOGIN_HOST}/**", timeout=30000)
            except Exception:
                logger.warning("Did not redirect to Microsoft login; URL: %s", page.url)

        # Step 3: Enter email
        logger.info("Entering email...")
        try:
            await page.wait_for_selector(
                'input[type="email"], input[name="loginfmt"]',
                timeout=15000,
            )
        except Exception:
            if await self._check_on_dashboard():
                logger.info("Already on dashboard — no email needed")
                await self._save_state()
                return True
            logger.error("Email field did not appear")
            return False

        email_field = page.locator('input[type="email"], input[name="loginfmt"]').first
        if await email_field.is_visible():
            await email_field.fill("")
            await email_field.type(self.email, delay=50)
        else:
            await page.fill('input[name="loginfmt"]', self.email)

        await page.click('input[type="submit"]')

        # Step 4: Enter password
        logger.info("Waiting for password field...")
        try:
            await page.wait_for_selector(
                'input[type="password"], input[name="passwd"]',
                timeout=30000,
            )
        except Exception:
            logger.error("Password field did not appear")
            return False

        await asyncio.sleep(1)
        pw_field = page.locator('input[type="password"], input[name="passwd"]').first
        if await pw_field.is_visible():
            await pw_field.fill(self.password)
        else:
            logger.error("Password field not visible")
            return False

        await page.click('input[type="submit"]')

        # Step 5: Handle MFA
        logger.info("Waiting for potential MFA prompt...")
        await asyncio.sleep(2)
        mfa_result: MfaResult = await handle_mfa(
            page,
            method=self.mfa_method,
            totp_secret=self.totp_secret,
            timeout=self.mfa_timeout,
            post_submit_timeout=self.mfa_post_submit_timeout,
        )

        if not mfa_result.success and mfa_result.status != MfaStatus.SKIPPED:
            current_url = page.url
            if LOGIN_HOST in current_url:
                logger.error(
                    "Still on login page after MFA — auth failed: %s",
                    mfa_result.reason,
                )
                return False
            logger.info("Left login page despite MFA handler reporting issues — proceeding")
        else:
            logger.info("MFA: %s — %s", mfa_result.status.value, mfa_result.reason)

        # Step 6: Wait for redirect away from login
        logger.info("Waiting for redirect back to AVD...")
        redirected = await self._wait_for_redirect_away_from_login(timeout=60.0)
        if not redirected:
            # Check for KMSI
            try:
                kmsi_yes = page.locator("#idSIButton9")
                if await kmsi_yes.is_visible(timeout=3000):
                    await kmsi_yes.click()
                    logger.info("Clicked 'Yes' on KMSI prompt")
                    redirected = await self._wait_for_redirect_away_from_login(timeout=30.0)
            except Exception:
                pass

        if not redirected:
            logger.error("Auth did not complete — still on login page")
            return False

        # Success
        await self._save_state()
        logger.info("Authentication complete")
        return True

    async def _reauthenticate(self) -> bool:
        """Re-authenticate after session expiration.

        Uses the existing browser context (cookies may still be valid enough
        to avoid a full login), but falls back to full auth flow.
        """
        logger.info("Re-authenticating (attempt #%d)...", self._reauth_count + 1)

        try:
            # Try a quick re-auth: just navigate to the entry point.
            # If cookies are still partially valid, we might get a shorter
            # re-auth flow.
            await self._page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            if await self._check_on_dashboard():
                logger.info("Quick re-auth succeeded — back on dashboard")
                await self._save_state()
                return True

            # Full re-auth
            success = await self._authenticate()
            if success:
                await self._navigate_to_dashboard()
                return True

            logger.error("Re-authentication failed")
            return False

        except Exception as exc:
            logger.exception("Re-authentication error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal: session health checks
    # ------------------------------------------------------------------

    async def _check_authenticated(self) -> bool:
        """Check if we appear to be authenticated (not on login page)."""
        if not self._page:
            return False
        try:
            url = self._page.url
            if LOGIN_HOST in url:
                return False
            if "windows.cloud.microsoft" in url:
                return True
            # If we're somewhere else, do a quick check
            return LOGIN_HOST not in url
        except Exception:
            return False

    async def _check_on_dashboard(self) -> bool:
        """Check if we're on the AVD dashboard (not login, not other pages)."""
        if not self._page:
            return False
        try:
            url = self._page.url
            return (
                LOGIN_HOST not in url
                and "windows.cloud.microsoft" in url
            )
        except Exception:
            return False

    async def _check_session_expired(self) -> bool:
        """Check if the current session has expired.

        Returns True if we've been redirected to the Microsoft login page
        or if the page shows session-expiration messaging.
        """
        if not self._page:
            return True

        try:
            url = self._page.url

            # Primary detection: redirected to login
            if LOGIN_HOST in url:
                logger.info("Session expired — redirected to Microsoft login")
                return True

            # Secondary: check for inline expiration messages
            for indicator in SESSION_EXPIRED_INDICATORS:
                try:
                    text_match = indicator.replace('text="', "").replace('"', "")
                    locator = self._page.get_by_text(text_match, exact=False)
                    if await locator.count() > 0:
                        el = locator.first
                        if await el.is_visible():
                            logger.info("Session expired — detected message: %s", text_match)
                            return True
                except Exception:
                    continue

            # Tertiary: page is no longer on AVD domain at all
            if "windows.cloud.microsoft" not in url and LOGIN_HOST not in url:
                # Could be an error page or logged-out state
                logger.debug("Page is outside AVD domain: %s", url[:100])

        except Exception as exc:
            logger.debug("Error checking session health: %s", exc)

        return False

    async def _ensure_on_dashboard(self) -> None:
        """Navigate to the dashboard if we're not already there."""
        if not await self._check_on_dashboard():
            await self._navigate_to_dashboard()

    async def _navigate_to_dashboard(self) -> None:
        """Navigate to the AVD dashboard and wait for it to load."""
        logger.info("Navigating to dashboard...")
        await self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # Wait for the SPA to finish rendering
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        logger.info("Dashboard loaded: %s", self._page.url[:100])

    # ------------------------------------------------------------------
    # Internal: VM interaction
    # ------------------------------------------------------------------

    async def _click_vm_by_text(self, vm_name: str) -> bool:
        """Find a VM on the dashboard by its name text and click it.

        The AVD dashboard renders resources as cards/tiles. We search for
        the VM name in the DOM, then find the nearest clickable parent.
        """
        logger.debug("Searching DOM for VM: %s", vm_name)

        # Strategy 0: Cloud PC cards — use the same extraction pattern
        # as _scrape_vms_from_dom, then match and click. This handles
        # Fluent UI v9 cards where the name is split across elements and
        # Playwright's get_by_text() can't find it as one contiguous text.
        try:
            cloud_pc_cards = self._page.locator('[data-testid*="cloudPC-card"]')
            card_count = await cloud_pc_cards.count()
            for i in range(min(card_count, 30)):
                card = cloud_pc_cards.nth(i)
                if not await card.is_visible():
                    continue
                # Extract the name the same way _scrape_vms_from_dom does
                try:
                    name_el = card.locator(
                        '[data-testid*="cloudPC-card-name"], '
                        '[class*="fui-Text"]'
                    ).first
                    card_name = ""
                    try:
                        if await name_el.count() > 0:
                            card_name = (await name_el.text_content()).strip()
                    except Exception:
                        pass
                    # Also try full card text as fallback
                    if not card_name:
                        card_name = (await card.text_content()).strip()
                        if card_name:
                            card_name = card_name.split("\n")[0][:100]

                    if card_name and (
                        vm_name.lower() == card_name.lower()
                        or vm_name.lower() in card_name.lower()
                        or card_name.lower() in vm_name.lower()
                    ):
                        logger.info("Matched Cloud PC card: %s", card_name)

                        # Try multiple interaction strategies for Cloud PC cards
                        clicked = False

                        # Strategy A: Click a "Connect"/"Start"/"Open" button inside card
                        for btn_text in ("Connect", "Start", "Open", "Launch",
                                         "connect", "start", "open", "launch"):
                            try:
                                btn = card.locator(
                                    f'button:has-text("{btn_text}"), '
                                    f'a:has-text("{btn_text}"), '
                                    f'[role="button"]:has-text("{btn_text}")'
                                ).first
                                if await btn.count() > 0 and await btn.is_visible():
                                    logger.info("Clicking '%s' button inside card", btn_text)
                                    await btn.click(timeout=5000)
                                    clicked = True
                                    break
                            except Exception:
                                continue

                        # Strategy B: Click any link inside the card
                        if not clicked:
                            link = card.locator("a").first
                            try:
                                if await link.count() > 0 and await link.is_visible():
                                    href = await link.get_attribute("href") or ""
                                    logger.info("Clicking card link → %s", href[:120])
                                    await link.click(timeout=5000)
                                    clicked = True
                            except Exception:
                                pass

                        # Strategy C: Click the card name element (often clickable)
                        if not clicked:
                            try:
                                name_btn = card.locator(
                                    '[data-testid*="cloudPC-card-name"], '
                                    '[class*="fui-Text"]'
                                ).first
                                if await name_btn.count() > 0 and await name_btn.is_visible():
                                    logger.info("Clicking card name element")
                                    await name_btn.click(timeout=5000)
                                    clicked = True
                            except Exception:
                                pass

                        # Strategy D: Click the card itself
                        if not clicked:
                            logger.info("Clicking card element as fallback")
                            await card.click(timeout=5000, force=True)
                            clicked = True

                        await asyncio.sleep(3)
                        # Check if a new page/tab opened
                        pages = self._context.pages
                        if len(pages) > 1:
                            logger.info("New page opened (total: %d pages)", len(pages))
                            return True
                        current_url = self._page.url
                        logger.info("Post-click URL: %s", current_url[:120])
                        if any(kw in current_url.lower() for kw in (
                            "webclient", "rdweb", "windows365",
                            "remotedesktop", "client",
                        )):
                            logger.info("Navigated to remote session: %s", current_url[:100])
                            return True
                        return True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Cloud PC card search failed: %s", exc)

        # Try to find the exact text on the page
        try:
            # Use Playwright's text locator to find the VM name
            text_locator = self._page.get_by_text(vm_name, exact=False)
            count = await text_locator.count()
            if count > 0:
                # Try each match — the first visible one that's on the dashboard
                for i in range(min(count, 10)):
                    el = text_locator.nth(i)
                    if await el.is_visible():
                        # Click the element itself — the dashboard should handle
                        # the click event on cards/tiles
                        try:
                            await el.click(timeout=5000)
                            await asyncio.sleep(3)
                            # Check if a new page/tab opened
                            pages = self._context.pages
                            if len(pages) > 1:
                                logger.info("New page opened (total: %d pages)", len(pages))
                                return True
                            # Check if we navigated to the webclient
                            current_url = self._page.url
                            if "webclient" in current_url or "rdweb" in current_url.lower():
                                logger.info("Navigated to webclient: %s", current_url[:100])
                                return True
                            return True
                        except Exception:
                            continue

                # Try clicking the parent element
                for i in range(min(count, 10)):
                    el = text_locator.nth(i)
                    if await el.is_visible():
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

        # Try VM selectors
        for sel in VM_SELECTORS:
            try:
                elements = self._page.locator(sel)
                count = await elements.count()
                for i in range(min(count, 20)):
                    el = elements.nth(i)
                    if await el.is_visible():
                        try:
                            text = await el.text_content()
                            if text and vm_name.lower() in text.lower():
                                await el.click(timeout=5000)
                                await asyncio.sleep(3)
                                pages = self._context.pages
                                if len(pages) > 1:
                                    logger.info(
                                        "VM '%s' opened — new page count: %d",
                                        vm_name,
                                        len(pages),
                                    )
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        return False

    async def _scrape_vms_from_dom(self) -> list[dict]:
        """Scrape VM names and metadata from the dashboard DOM.

        Handles both Windows App Cloud PC cards (Fluent UI v9,
        data-testid=\"cloudPC-card-{guid}\") and legacy AVD resource cards.
        Extracts name, status (Running/Stopped/etc.), and resource specs
        where available.
        """
        vms = []
        seen_names = set()

        if not self._page:
            return vms

        # Strategy 1: Cloud PC cards — the most specific, highest-signal pattern
        for sel in ('[data-testid*="cloudPC-card"]',):
            try:
                cards = self._page.locator(sel)
                count = await cards.count()
                for i in range(min(count, 30)):
                    card = cards.nth(i)
                    if not await card.is_visible():
                        continue
                    try:
                        name = ""
                        status = ""
                        specs = ""

                        # Extract name from inside the card
                        name_el = card.locator(
                            '[data-testid*="cloudPC-card-name"], '
                            '[class*="fui-Text"]'
                        ).first
                        try:
                            if await name_el.count() > 0:
                                name = (await name_el.text_content()).strip()
                        except Exception:
                            pass

                        # Extract status badge
                        status_el = card.locator(
                            '[class*="StatusBadge"], '
                            '[class*="badge"], '
                            '[class*="status"]'
                        ).first
                        try:
                            if await status_el.count() > 0:
                                status = (await status_el.text_content()).strip()
                        except Exception:
                            pass

                        # Extract specs / description
                        specs_el = card.locator(
                            '[class*="Text"][class*="secondary"], '
                            '[class*="fui-Text"][class*="caption"]'
                        ).last
                        try:
                            if await specs_el.count() > 0:
                                specs = (await specs_el.text_content()).strip()
                        except Exception:
                            pass

                        # Extract resource ID from any link or data attribute
                        resource_id = ""
                        # Strategy A: card's data-testid often contains the GUID
                        #   e.g. data-testid="cloudPC-card-bc5e8a38-..."
                        try:
                            testid = await card.get_attribute("data-testid") or ""
                            if testid:
                                extracted = self._extract_guid(testid)
                                if extracted:
                                    resource_id = extracted
                        except Exception:
                            pass
                        # Strategy B: link href with resourceId query param
                        if not resource_id:
                            link = card.locator("a").first
                            try:
                                if await link.count() > 0:
                                    href = await link.get_attribute("href") or ""
                                    # Look for resourceId query param
                                    import re as _re
                                    m = _re.search(r"resourceId=([^&]+)", href)
                                    if m:
                                        resource_id = m.group(1)
                                    else:
                                        resource_id = self._extract_guid(href)
                            except Exception:
                                pass
                        # Strategy C: any descendant element with a data-resource-id attribute
                        if not resource_id:
                            try:
                                data_el = card.locator("[data-resource-id]").first
                                if await data_el.count() > 0:
                                    resource_id = await data_el.get_attribute("data-resource-id") or ""
                            except Exception:
                                pass

                        # Fallback: try name from full card text if individual
                        # extraction failed
                        if not name:
                            text = (await card.text_content()).strip()
                            if text:
                                name = text.split("\n")[0][:100]

                        if name and len(name) > 1 and name not in seen_names:
                            seen_names.add(name)
                            vms.append({
                                "name": name,
                                "kind": "CloudPC",
                                "workspace": "",
                                "resource_id": resource_id,
                                "status": status or "Unknown",
                                "specs": specs,
                            })
                    except Exception:
                        continue
                if vms:
                    break
            except Exception:
                continue

        if vms:
            return vms

        # Strategy 2: Legacy AVD resource cards
        for sel in VM_SELECTORS:
            try:
                elements = self._page.locator(sel)
                count = await elements.count()
                for i in range(min(count, 30)):
                    el = elements.nth(i)
                    if not await el.is_visible():
                        continue
                    try:
                        text = (await el.text_content()).strip()
                        href = await el.get_attribute("href") or ""
                        # Skip empty text (e.g. search inputs that match
                        # broad selectors like "[data-testid*=\"resource\"]")
                        if not text or len(text) <= 1:
                            continue
                        name = text.split("\n")[0][:100]
                        if name not in seen_names:
                            seen_names.add(name)
                            vms.append({
                                "name": name,
                                "kind": "desktop",
                                "workspace": "",
                                "resource_id": self._extract_guid(href),
                                "status": "Unknown",
                                "specs": "",
                            })
                    except Exception:
                        continue
                if vms:
                    break
            except Exception:
                continue

        return vms

    @staticmethod
    def _parse_feed_response(data: dict | list) -> list[dict]:
        """Parse feed discovery API response into VM list."""
        vms = []

        # Handle plain list of resources directly
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    # Check if this is a workspace wrapper or a bare resource
                    if "resources" in item:
                        # Workspace wrapper (list of workspaces without a 'value' key)
                        workspace = item.get("workspace", {})
                        workspace_name = workspace.get("name", workspace.get("friendlyName", "Unknown"))
                        for resource in item.get("resources", []):
                            vms.append({
                                "name": resource.get("name", resource.get("friendlyName", "Unnamed")),
                                "kind": resource.get("resourceType", resource.get("type", "unknown")),
                                "workspace": workspace_name,
                                "resource_id": resource.get("resourceId", resource.get("id", "")),
                                "icon": resource.get("icon", ""),
                            })
                    else:
                        # Bare resource
                        vms.append({
                            "name": item.get("name", item.get("friendlyName", "Unnamed")),
                            "kind": item.get("resourceType", item.get("type", "unknown")),
                            "workspace": item.get("workspaceName", ""),
                            "resource_id": item.get("resourceId", item.get("id", "")),
                        })
            return vms

        # Standard response: {"value": [...]}
        resources_list = data.get("value", []) if isinstance(data, dict) else []

        for workspace_entry in resources_list:
            workspace = workspace_entry.get("workspace", {})
            workspace_name = workspace.get("name", workspace.get("friendlyName", "Unknown"))
            for resource in workspace_entry.get("resources", []):
                vms.append({
                    "name": resource.get("name", resource.get("friendlyName", "Unnamed")),
                    "kind": resource.get("resourceType", resource.get("type", "unknown")),
                    "workspace": workspace_name,
                    "resource_id": resource.get("resourceId", resource.get("id", "")),
                    "icon": resource.get("icon", ""),
                })
        return vms

    @staticmethod
    def _extract_guid(text: str) -> str:
        """Extract a GUID from a string."""
        import re
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        )
        return match.group(0) if match else ""

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    async def _wait_for_redirect_away_from_login(self, timeout: float = 60.0) -> bool:
        """Wait until the page leaves login.microsoftonline.com."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            url = self._page.url
            if LOGIN_HOST not in url:
                logger.info("Left login page → %s", url[:100])
                return True
            await asyncio.sleep(1)
        return False

    async def _save_state(self) -> None:
        """Persist browser state to disk."""
        try:
            await self._context.storage_state(path=str(self.storage_state_path))
            logger.debug("Saved auth state to %s", self.storage_state_path)
        except Exception as exc:
            logger.warning("Could not save auth state: %s", exc)


# ---------------------------------------------------------------------------
# Interactive command loop
# ---------------------------------------------------------------------------

async def _interactive_loop(manager: AVDSessionManager) -> None:
    """Read commands from stdin in a background task."""
    loop = asyncio.get_event_loop()

    print("\nInteractive mode — type commands (or 'help'):", flush=True)

    while manager._running:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            break

        cmd = line.strip().lower()

        if cmd in ("help", "h", "?"):
            print(
                "Commands:\n"
                "  list              — list available VMs\n"
                "  start <name|n>    — start a VM by name or index number\n"
                "  connect <name>    — connect to a VM by name\n"
                "  status            — show session status\n"
                "  reauth            — force re-authentication\n"
                "  quit / exit       — shutdown",
                flush=True,
            )
        elif cmd in ("list", "ls"):
            vms = await manager.list_vms()
            if vms:
                print(f"\n{len(vms)} resource(s) available:\n", flush=True)
                for i, vm in enumerate(vms, 1):
                    print(f"  [{i}] {vm['name']}", flush=True)
                    if vm.get("status"):
                        print(f"      Status:    {vm['status']}", flush=True)
                    if vm.get("kind") and vm.get("kind") != "desktop":
                        print(f"      Kind:      {vm['kind']}", flush=True)
                    if vm.get("workspace"):
                        print(f"      Workspace: {vm['workspace']}", flush=True)
                    if vm.get("specs"):
                        print(f"      Specs:     {vm['specs']}", flush=True)
                    if vm.get("resource_id"):
                        print(f"      Resource:  {vm['resource_id']}", flush=True)
                    print(flush=True)
            else:
                print(
                    "No VMs found. Check that your account has provisioned resources\n"
                    "and that the dashboard has finished loading.", flush=True,
                )
        elif cmd.startswith("start ") or cmd == "start":
            selector = cmd[6:].strip()  # everything after "start "
            vms = await manager.list_vms()
            vm_name = _resolve_vm_selection(selector, vms)
            if vm_name is None:
                if selector:
                    print(
                        f"Could not resolve VM: '{selector}'. "
                        "Use 'list' to see available VMs.",
                        flush=True,
                    )
                continue
            print(f"Starting VM: {vm_name}", flush=True)
            ok = await manager.connect_to_vm(vm_name)
            print(
                f"{'Connected to' if ok else 'Failed to connect to'} VM: {vm_name}",
                flush=True,
            )
        elif cmd.startswith("connect ") or cmd.startswith("vm "):
            vm_name = line.strip().split(" ", 1)[1] if " " in line.strip() else ""
            if vm_name:
                ok = await manager.connect_to_vm(vm_name)
                print(
                    f"{'Connected to' if ok else 'Failed to find'} VM: {vm_name}",
                    flush=True,
                )
            else:
                print("Usage: connect <vm-name>", flush=True)
        elif cmd in ("status", "st"):
            state = await manager.get_state()
            print(
                f"\nSession Status:\n"
                f"  Authenticated: {state.authenticated}\n"
                f"  On Dashboard:  {state.on_dashboard}\n"
                f"  URL:           {state.current_url[:120]}\n"
                f"  VM Count:      {state.vm_count}\n"
                f"  Re-auths:      {state.reauth_count}\n"
                f"  Uptime:        {state.uptime_seconds:.0f}s",
                flush=True,
            )
        elif cmd == "reauth":
            print("Forcing re-authentication...", flush=True)
            ok = await manager._reauthenticate()
            print(f"Re-auth {'succeeded' if ok else 'failed'}", flush=True)
        elif cmd in ("quit", "exit", "q"):
            break
        else:
            print(f"Unknown command: {cmd!r} (type 'help')", flush=True)

    manager._running = False


# ---------------------------------------------------------------------------
# VM selection helper
# ---------------------------------------------------------------------------

def _resolve_vm_selection(
    selector: str,
    vms: list[dict],
) -> str | None:
    """Resolve a VM selector to a VM name.

    * Numeric selector → select by list index (1-based, from --list output)
    * String selector  → fuzzy name match
    * Empty string     → interactive numbered prompt

    Returns the resolved VM name, or None if selection failed/aborted.
    """
    if not vms:
        print("No VMs available to select from.", flush=True)
        return None

    # --- Numeric index (from --list output) ---
    try:
        idx = int(selector)
        if 1 <= idx <= len(vms):
            return vms[idx - 1]["name"]
        print(
            f"Index {idx} is out of range (1–{len(vms)}). "
            "Run with --list to see available VMs.",
            flush=True,
        )
        return None
    except ValueError:
        pass  # not a number — treat as name

    # --- Name match ---
    if selector.strip():
        sel_lower = selector.lower()
        # Exact match first
        for vm in vms:
            if vm["name"].lower() == sel_lower:
                return vm["name"]
        # Partial match
        matches = [
            vm["name"] for vm in vms if sel_lower in vm["name"].lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"Ambiguous name '{selector}' — matches: "
                + ", ".join(f"{i}) {m}" for i, m in enumerate(matches, 1)),
                flush=True,
            )
            return None
        print(f"No VM found matching '{selector}'.", flush=True)
        return None

    # --- Interactive prompt (no selector given) ---
    print(f"\n{len(vms)} resource(s) available:\n")
    for i, vm in enumerate(vms, 1):
        status = vm.get("status", "Unknown")
        kind = vm.get("kind", "")
        kind_str = f" [{kind}]" if kind and kind != "desktop" else ""
        print(f"  [{i}] {vm['name']}{kind_str}  ({status})")
    print()

    while True:
        try:
            choice = input("Select VM number (or 'q' to quit): ").strip()
            if choice.lower() in ("q", "quit", ""):
                return None
            idx = int(choice)
            if 1 <= idx <= len(vms):
                return vms[idx - 1]["name"]
            print(f"Enter a number between 1 and {len(vms)}.", flush=True)
        except (ValueError, EOFError):
            print("Enter a number (or 'q' to quit).", flush=True)
        except KeyboardInterrupt:
            return None


# ---------------------------------------------------------------------------
# Native desktop backend (pywebview JS API bridge)
# ---------------------------------------------------------------------------

class _DesktopBackend:
    """pywebview JS API — manages AVD session in a background event loop.

    Creates and owns the AVDSessionManager. The session lifecycle runs in
    a dedicated asyncio event loop on a background thread. pywebview
    blocks the main thread.
    """

    def __init__(self, config: dict, headed: bool = False, state_path: str = "./auth-state.json"):
        self._config = config
        self._headed = headed
        self._state_path = state_path
        self._manager: "AVDSessionManager | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: str | None = None

    # ---- Public API (exposed to JS via pywebview) ----

    def get_state(self) -> dict:
        """Return current session state."""
        try:
            state = self._run_coro(self._manager.get_state())
            return {
                "authenticated": state.authenticated,
                "on_dashboard": state.on_dashboard,
                "vm_count": state.vm_count,
                "reauth_count": state.reauth_count,
                "uptime_seconds": state.uptime_seconds,
            }
        except Exception as exc:
            return {"authenticated": False, "vm_count": 0, "uptime_seconds": 0, "error": str(exc)}

    def list_vms(self) -> list[dict]:
        """Return available VMs with metadata."""
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
            return [{"name": f"Error: {exc}", "kind": "", "status": "Error"}]

    def connect_vm(self, vm_name: str) -> dict:
        """Connect to a VM. Opens webclient in default browser if resource_id found."""
        import webbrowser as _wb

        try:
            # Find resource_id from VM list
            vms = self._run_coro(self._manager.list_vms())
            resource_id = ""
            for vm in vms:
                if vm_name.lower() in vm.get("name", "").lower():
                    resource_id = vm.get("resource_id", "")
                    break

            if resource_id:
                url = f"https://windows.cloud.microsoft/webclient/index.html?resourceId={resource_id}"
                logger.info("Opening webclient in browser → %s", url)
                _wb.open(url, new=2)  # new=2 = new tab
                return {"status": "connected", "vm_name": vm_name, "method": "webclient"}

            # Fallback: Playwright-based connection
            logger.info("No resource_id — connecting via Playwright")
            ok = self._run_coro(self._manager.connect_to_vm(vm_name))
            if ok:
                return {"status": "connected", "vm_name": vm_name, "method": "playwright"}
            return {"status": "error", "message": f"Could not find VM: {vm_name}"}
        except Exception as exc:
            logger.exception("connect_vm failed")
            return {"status": "error", "message": str(exc)}

    # ---- Lifecycle ----

    def start_background(self) -> threading.Thread:
        """Start the session in a background thread. Returns the thread."""
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return thread

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until the session is authenticated and ready."""
        return self._ready.wait(timeout)

    def stop(self) -> None:
        """Shut down the session."""
        if self._loop and self._manager:
            try:
                future = asyncio.run_coroutine_threadsafe(self._manager.stop(), self._loop)
                future.result(timeout=10)
            except Exception:
                pass

    @property
    def error(self) -> str | None:
        return self._error

    # ---- Internal ----

    def _run(self) -> None:
        """Create session, authenticate, and run keep-alive (blocking)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_session())
        except Exception as exc:
            logger.exception("Session thread crashed")
            self._error = str(exc)
        finally:
            self._loop.close()

    async def _run_session(self) -> None:
        """Create AVDSessionManager, authenticate, start keep-alive."""
        manager = AVDSessionManager(
            email=self._config["email"],
            password=self._config["password"],
            mfa_method=self._config["mfa_method"],
            totp_secret=self._config["totp_secret"],
            mfa_timeout=self._config["mfa_timeout"],
            mfa_post_submit_timeout=self._config["mfa_post_submit_timeout"],
            storage_state_path=self._state_path,
            poll_interval=self._config["poll_interval"],
            reauth_timeout=self._config["reauth_timeout"],
            headed=self._headed,
        )

        logger.info("Starting AVD session...")
        ok = await manager.start()
        if not ok:
            self._error = "Failed to start session — check credentials"
            logger.error(self._error)
            self._ready.set()
            return

        self._manager = manager
        logger.info("AVD session ready")
        self._ready.set()

        try:
            await manager.run()
        except asyncio.CancelledError:
            pass

    def _run_coro(self, coro):
        """Schedule a coroutine on the background loop, blocking the caller."""
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("Event loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="AVD persistent session manager — keeps browser session alive",
    )
    parser.add_argument(
        "--vm", "-c",
        help="Connect to a VM by name (fuzzy match)",
    )
    parser.add_argument(
        "--no-keep",
        action="store_true",
        help="Connect to VM and exit (do not keep session alive)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available VMs and exit",
    )
    parser.add_argument(
        "--start",
        nargs="?",
        const="",
        default=None,
        help="Start a VM by name, list index (e.g. --start 1), or pick interactively (--start alone)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=os.getenv("AVD_HEADED", "").lower() in ("1", "true", "yes"),
        help="Show browser window (use with --start for fully interactive VM sessions)",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Read commands from stdin (list, connect, status, reauth, quit)",
    )
    parser.add_argument(
        "--state", "-s",
        default=os.getenv("AVD_STORAGE_STATE_PATH", "./auth-state.json"),
        help="Path to save/load auth state",
    )
    # Build resolution help from PRESETS for single-source-of-truth
    _res_help_parts = []
    for name in ("desktop", "laptop", "hd", "fhd", "qhd", "4k"):
        if name in PRESETS:
            p = PRESETS[name]
            _res_help_parts.append(f"'{name}' ({p['width']}x{p['height']})")
    _res_help = (
        "Browser viewport: " + ", ".join(_res_help_parts)
        + ", 'WxH' like '1920x1080', or 'auto' (default: desktop)"
    )
    parser.add_argument(
        "--resolution", "-r",
        default=os.getenv("AZURE_RESOLUTION"),
        help=_res_help,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f"HTTP port for dashboard mode (default: {DEFAULT_DASHBOARD_PORT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = _get_config()
    has_cli_mode = any([args.list, args.vm, args.start is not None, args.interactive])

    # If running in CLI mode, create and start the session manager.
    # In desktop mode, _DesktopBackend creates its own session.
    if has_cli_mode:
        manager = AVDSessionManager(
            email=config["email"],
            password=config["password"],
            mfa_method=config["mfa_method"],
            totp_secret=config["totp_secret"],
            mfa_timeout=config["mfa_timeout"],
            mfa_post_submit_timeout=config["mfa_post_submit_timeout"],
            storage_state_path=args.state,
            poll_interval=config["poll_interval"],
            reauth_timeout=config["reauth_timeout"],
            headed=args.headed,
            resolution=args.resolution,
        )

        # Start session
        logger.info("Starting AVD session...")
        ok = await manager.start()
        if not ok:
            logger.error("Failed to start session")
            await manager.stop()
            sys.exit(1)

        logger.info("Session started successfully")

    # --list mode: show VMs and exit
    if args.list:
        vms = await manager.list_vms()
        if vms:
            print(f"\n{len(vms)} resource(s) available:\n")
            for i, vm in enumerate(vms, 1):
                print(f"  [{i}] {vm['name']}")
                if vm.get("status"):
                    print(f"      Status:    {vm['status']}")
                if vm.get("kind") and vm.get("kind") != "desktop":
                    print(f"      Kind:      {vm['kind']}")
                if vm.get("workspace"):
                    print(f"      Workspace: {vm['workspace']}")
                if vm.get("specs"):
                    print(f"      Specs:     {vm['specs']}")
                if vm.get("resource_id"):
                    print(f"      Resource:  {vm['resource_id']}")
                print()
            print(
                "Run:  python avd_session.py --start <n> --headed "
                "(to start a VM by index)\n"
                "      python avd_session.py --start --headed "
                "(to pick interactively)\n",
            )
        else:
            print(
                "\nNo VMs found.\n\n"
                "Possible causes:\n"
                "  - Your account has no provisioned Cloud PCs or AVD resources.\n"
                "  - The dashboard has not finished loading — try again.\n"
                "  - The DOM structure has changed — use --verbose to see debug logs.\n"
                "  - Permission / RBAC: verify your account is assigned to a workspace.\n"
            )
        await manager.stop()
        return

    # --vm mode: connect to a VM
    if args.vm:
        ok = await manager.connect_to_vm(args.vm)
        if ok:
            print(f"Connected to VM: {args.vm}")
        else:
            print(f"Could not find VM: {args.vm}")
            # Keep the session alive so the user can investigate
        if args.no_keep:
            await manager.stop()
            return

    # --start mode: select a VM (by name, index, or interactively) and connect
    if args.start is not None:
        vms = await manager.list_vms()
        vm_name = _resolve_vm_selection(args.start, vms)
        if vm_name is None:
            await manager.stop()
            sys.exit(1)

        print(f"Starting VM: {vm_name}")
        ok = await manager.connect_to_vm(vm_name)
        if not ok:
            print(f"Could not connect to VM: {vm_name}")
            await manager.stop()
            sys.exit(1)

        print(f"Connected to VM: {vm_name}")

        if args.no_keep:
            await manager.stop()
            return

        # With --headed, keep the session alive so the user can interact
        if args.headed:
            print(
                "\nVM session started in headed mode — browser window is interactive.\n"
                "You can now interact with the VM through the browser.\n"
                "Close the browser window or press Ctrl+C to exit.\n",
                flush=True,
            )
        else:
            print(
                "\nVM session started in headless mode — session kept alive.\n"
                "Press Ctrl+C to exit.\n",
                flush=True,
            )

        # Keep the session running (detect expiration, auto-reauth)
        try:
            await manager.run()
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down")
        finally:
            await manager.stop()
        return

    # --interactive mode
    if args.interactive:
        loop_task = asyncio.create_task(_interactive_loop(manager))
        session_task = asyncio.create_task(manager.run())
        done, _ = await asyncio.wait(
            [loop_task, session_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in [loop_task, session_task]:
            if not task.done():
                task.cancel()
        await manager.stop()
        return

    # Default: launch Playwright in headed mode as the native app window.
    # The Playwright Chromium IS the desktop app — authenticated, devtools-ready,
    # VM sessions open as new tabs. No Qt, no cookie syncing, no external browser.
    import subprocess as _sp
    import signal as _signal

    # Start Flask server
    _dashboard_app = _get_dashboard_app()
    _flask = threading.Thread(
        target=lambda: _dashboard_app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False),
        daemon=True,
    )
    _flask.start()

    # Create the AVDSessionManager on the main event loop (headed mode for VM tabs)
    manager = AVDSessionManager(
        email=config["email"],
        password=config["password"],
        mfa_method=config["mfa_method"],
        totp_secret=config["totp_secret"],
        mfa_timeout=config["mfa_timeout"],
        mfa_post_submit_timeout=config["mfa_post_submit_timeout"],
        storage_state_path=args.state,
        poll_interval=config["poll_interval"],
        reauth_timeout=config["reauth_timeout"],
        headed=True,  # Show the browser — this IS the app window
        resolution=args.resolution,
    )

    logger.info("Starting AVD session (headed)...")
    ok = await manager.start()
    if not ok:
        logger.error("Failed to start session")
        sys.exit(1)

    logger.info("Session ready — wiring API before dashboard loads")

    # Wire dashboard API globals BEFORE the page loads
    global _dashboard_session, _dashboard_loop
    _dashboard_session = manager
    _dashboard_loop = asyncio.get_running_loop()

    # Open a new app-mode page for the dashboard (no browser chrome)
    _dash_page = await manager._context.new_page()
    await _dash_page.goto(f"http://127.0.0.1:{args.port}", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Close the initial page (which is on the AVD dashboard)
    if manager._page is not _dash_page:
        await manager._page.close()
        manager._page = _dash_page

    print(f"\n  AVD Dashboard ready. VM sessions open in this app window.\n  Press Ctrl+C to exit.\n", flush=True)

    # Keep session alive — detects expiry, auto-reauths
    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(_main())
