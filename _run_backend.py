#!/usr/bin/env python3
"""Backend for Tauri AVD Dashboard."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

_WORKEE_DIR = Path(__file__).resolve().parent
_ENV_PATH = _WORKEE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()

os.chdir(str(_WORKEE_DIR))
logger = logging.getLogger("avd_backend")

app = Flask(__name__)

SIMPLE_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AVD Dashboard</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;margin:0;padding:0}
header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:18px;font-weight:600;color:#58a6ff;margin:0}
#status{font-size:13px;color:#8b949e}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.online{background:#3fb950}.dot.offline{background:#f85149}.dot.waiting{background:#d29922}
main{max-width:1200px;margin:0 auto;padding:24px}
.bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.bar h2{font-size:14px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin:0}
.bar button{padding:6px 14px;background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;cursor:pointer;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;transition:border-color .15s}
.card:hover{border-color:#58a6ff}
.card h3{font-size:16px;font-weight:600;margin:0 0 4px 0}
.card .meta{font-size:12px;color:#8b949e;margin-bottom:14px}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:12px;font-weight:600;margin-right:8px}
.badge.on{background:rgba(63,185,80,.15);color:#3fb950}
.badge.off{background:rgba(139,148,158,.15);color:#8b949e}
.badge.cpc{background:rgba(88,166,255,.15);color:#58a6ff}
.card button{width:100%;padding:10px;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;background:#58a6ff;color:#fff}
.card button:hover{opacity:.85}
.empty{text-align:center;padding:48px 24px;color:#8b949e}
</style>
</head>
<body>
<header><h1>AVD VM Dashboard</h1><div id="status"><span class="dot waiting"></span> Connecting...</div></header>
<main>
<div class="bar"><h2>Virtual Machines</h2><button onclick="load()">Refresh</button></div>
<div class="grid" id="grid"><div class="empty">Loading...</div></div>
</main>
<script>
function el(id){return document.getElementById(id)}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

async function check(){
  try{
    var s=await fetch('/api/state').then(function(r){return r.json()});
    if(s.authenticated){el('status').innerHTML='<span class="dot online"></span> Ready &middot; '+s.vm_count+' VM(s)';load()}
    else{el('status').innerHTML='<span class="dot waiting"></span> Authenticating...';setTimeout(check,3000)}
  }catch(e){el('status').innerHTML='<span class="dot offline"></span> Offline';setTimeout(check,5000)}
}

async function load(){
  try{
    var d=await fetch('/api/vms').then(function(r){return r.json()});
    var vms=d.vms||[];
    var g=el('grid');
    if(!vms.length){g.innerHTML='<div class="empty">No VMs found</div>';return}
    g.innerHTML=vms.map(function(vm){
      var s=vm.status||'Unknown';
      var run=s.toLowerCase().indexOf('running')>=0;
      var c=run?'on':(vm.kind==='CloudPC'?'cpc':'off');
      return'<div class="card"><h3>'+esc(vm.name)+'</h3>'
        +'<div class="meta"><span class="badge '+c+'">'+esc(s)+'</span>'
        +(vm.kind?' <span>'+esc(vm.kind)+'</span>':'')
        +(vm.specs?' <span>&middot; '+esc(vm.specs)+'</span>':'')
        +'</div>'
        +'<button onclick="connect(\x27'+esc(vm.name)+'\x27)">Connect</button>'
        +'</div>';
    }).join('');
  }catch(e){el('grid').innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>'}
}

function connect(name){
  if(window.avdAPI){
    window.avdAPI.connectVm(name)
    .then(function(r){})
    .catch(function(e){alert('Error: '+e.message||e)})
  } else {
    // Fallback: open in system browser
    fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vm_name:name})})
    .then(function(r){return r.json()})
    .then(function(d){if(d.url)window.open(d.url,'_blank')})
    .catch(function(e){alert('Error: '+e.message)})
  }
}

check();
setInterval(check,15000);
</script>
</body>
</html>"""


# ---- Global state ----
_session_manager = None
_session_loop = None


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/connect", methods=["OPTIONS"])
@app.route("/api/state", methods=["OPTIONS"])
@app.route("/api/vms", methods=["OPTIONS"])
def _options():
    return "", 204


@app.route("/")
def index():
    return SIMPLE_DASHBOARD, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/state")
def api_state():
    if _session_manager is None:
        return jsonify({"authenticated": False, "vm_count": 0})
    try:
        state = _run_async(_session_manager.get_state())
        # Also refresh VM list so count is accurate
        vms = _run_async(_session_manager.list_vms())
        return jsonify({
            "authenticated": state.authenticated,
            "vm_count": len(vms),
            "uptime_seconds": state.uptime_seconds,
        })
    except Exception as exc:
        return jsonify({"authenticated": False, "vm_count": 0, "error": str(exc)})


@app.route("/api/vms")
def api_vms():
    if _session_manager is None:
        return jsonify({"error": "Session not started"}), 503
    try:
        vms = _run_async(_session_manager.list_vms())
        return jsonify({
            "vms": [{
                "name": vm.get("name", ""),
                "kind": vm.get("kind", ""),
                "resource_id": vm.get("resource_id", ""),
                "status": vm.get("status", "Unknown"),
                "specs": vm.get("specs", ""),
            } for vm in vms]
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cookies")
def api_cookies():
    """Return Playwright auth cookies + sessionStorage for injection into Electron."""
    if _session_manager is None:
        return jsonify({"error": "Session not started"}), 503
    try:
        cookies = _run_async(_session_manager._context.cookies())
        session_storage = {}
        try:
            ss = _run_async(_session_manager._page.evaluate(
                "()=>{const o={};for(let i=0;i<sessionStorage.length;i++)"
                "{const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k)}return o;}"
            ))
            session_storage = ss or {}
        except Exception:
            pass
        return jsonify({
            "cookies": [{"name": c["name"], "value": c["value"],
                          "domain": c.get("domain", ""),
                          "path": c.get("path", "/"),
                          "secure": c.get("secure", False)}
                         for c in (cookies or [])],
            "session_storage": session_storage,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Return webclient URL for the VM."""
    if _session_manager is None:
        return jsonify({"error": "Session not started"}), 503
    body = request.get_json(silent=True) or {}
    vm_name = body.get("vm_name", "").strip()
    if not vm_name:
        return jsonify({"error": "Missing vm_name"}), 400
    try:
        vms = _run_async(_session_manager.list_vms())
        for vm in vms:
            if vm_name.lower() in vm.get("name", "").lower():
                rid = vm.get("resource_id", "")
                if rid:
                    cookies = _run_async(_session_manager._context.cookies())
                    session_storage = {}
                    try:
                        ss = _run_async(_session_manager._page.evaluate(
                            "() => { const o={}; for(let i=0;i<sessionStorage.length;i++)"
                            "{const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k)}return o;}"
                        ))
                        session_storage = ss or {}
                    except Exception:
                        pass
                    return jsonify({
                        "url": f"https://windows.cloud.microsoft/webclient/index.html?resourceId={rid}",
                        "cookies": [{"name": c["name"], "value": c["value"],
                                      "domain": c.get("domain", ""),
                                      "path": c.get("path", "/"),
                                      "secure": c.get("secure", False)}
                                     for c in (cookies or [])],
                        "session_storage": session_storage,
                    })
        return jsonify({"error": "VM not found"}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _run_async(coro):
    if _session_loop is None:
        raise RuntimeError("Event loop not available")
    future = asyncio.run_coroutine_threadsafe(coro, _session_loop)
    return future.result(timeout=30)


# ---- Main ----

def main():
    global _session_manager, _session_loop

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    email = os.getenv("AVD_EMAIL")
    password = os.getenv("AVD_PASSWORD")
    if not email or not password:
        logger.error("AVD_EMAIL and AVD_PASSWORD not set")
        sys.exit(1)

    sys.path.insert(0, str(_WORKEE_DIR))
    from avd_session import AVDSessionManager

    loop = asyncio.new_event_loop()
    _session_loop = loop

    manager = AVDSessionManager(
        email=email,
        password=password,
        mfa_method=os.getenv("AVD_MFA_METHOD", "auto"),
        totp_secret=os.getenv("AVD_TOTP_SECRET") or None,
        mfa_timeout=float(os.getenv("AVD_MFA_TIMEOUT", "120")),
        storage_state_path=os.getenv("AVD_STORAGE_STATE_PATH", "./auth-state.json"),
        poll_interval=float(os.getenv("AVD_SESSION_POLL_INTERVAL", "15")),
        headed=False,
    )

    async def run_session():
        global _session_manager
        while True:
            try:
                ok = await manager.start()
                if not ok:
                    logger.error("Session start failed — retrying in 10s")
                    await asyncio.sleep(10)
                    continue
                _session_manager = manager
                logger.info("Session ready")

                # Dismiss tour popups
                try:
                    await asyncio.sleep(3)
                    page = manager._page
                    for text in ("Skip", "Skip tour", "Maybe later", "Got it", "OK", "Close", "Dismiss"):
                        btn = page.get_by_text(text, exact=False).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(0.5)
                except Exception:
                    pass

                await manager.run()
            except Exception as exc:
                logger.warning("Session loop crashed (%s) — restarting in 5s", exc)
                _session_manager = None
                await asyncio.sleep(5)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_session())

    threading.Thread(target=run_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
