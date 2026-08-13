#!/usr/bin/env python3
"""
ndi_control.py — Programmatic control of OBS NDI screen sharing.

Connects to OBS Studio via its built-in obs-websocket plugin (available since
OBS 28) and provides commands to start/stop NDI output, switch scenes, and
monitor stream health.

Prerequisites:
    - OBS Studio 28+ running with the NDI_ScreenShare profile
      (obs --profile NDI_ScreenShare --collection NDI_ScreenShare)
    - obs-websocket enabled (Tools → obs-websocket Settings → Enable)
    - Default websocket password is blank; set one via the OBS dialog

Usage:
    python ndi_control.py start              # start desktop capture + NDI output
    python ndi_control.py stop               # stop NDI output (OBS stays running)
    python ndi_control.py status             # show OBS/NDI state
    python ndi_control.py scene <name>       # switch to a different scene
    python ndi_control.py display <n>        # switch capture to display N
    python ndi_control.py window <title>     # capture a specific window by title
    python ndi_control.py --host localhost --port 4455 --password secret start

Environment variables (optional; fall back to defaults):
    OBS_WS_HOST       websocket host   (default: localhost)
    OBS_WS_PORT       websocket port   (default: 4455)
    OBS_WS_PASSWORD   websocket auth   (default: empty — no password)
"""

import argparse
import json
import os
import struct
import hashlib
import base64
import sys
import time
from typing import Any


# ---- Minimal obs-websocket client (no external deps) ------------------------
# OBS WebSocket v5.0 protocol (RFC 6455 framing + JSON messages).

WEBSOCKET_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class OBSWebSocketError(Exception):
    """Raised when an OBS request returns an error status."""


class OBSClient:
    """Minimal blocking WebSocket client for obs-websocket v5."""

    def __init__(self, host: str = "localhost", port: int = 4455, password: str = ""):
        self.host = host
        self.port = port
        self.password = password
        self._sock = None
        self._msg_id = 0

    # -- connection -----------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        """Open a WebSocket connection and perform the auth handshake."""
        import socket

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((self.host, self.port))

        # WebSocket upgrade handshake
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(request.encode())

        response = self._sock.recv(4096).decode(errors="replace")
        if "101 Switching Protocols" not in response:
            raise OBSWebSocketError(f"WebSocket upgrade failed: {response.split(chr(13))[0]}")

        # Authenticate (v5 protocol: identify with RPC 1 + password)
        hello = self._recv_json()
        if hello.get("op") != 0:
            raise OBSWebSocketError(f"Expected Hello (op 0), got op {hello.get('op')}")

        auth = ""
        if self.password:
            challenge = hello["d"]["authentication"]["challenge"]
            salt = hello["d"]["authentication"]["salt"]
            secret = base64.b64encode(
                hashlib.sha256((self.password + salt).encode()).digest()
            ).decode()
            auth_secret = base64.b64encode(
                hashlib.sha256((secret + challenge).encode()).digest()
            ).decode()
            auth = auth_secret

        self._send_json({
            "op": 1,
            "d": {
                "rpcVersion": 1,
                "authentication": auth,
            },
        })
        identified = self._recv_json()
        if identified.get("op") != 2:
            raise OBSWebSocketError(f"Auth failed: {identified}")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(2)
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    # -- request/response -----------------------------------------------------

    def call(self, request_type: str, request_data: dict | None = None) -> dict:
        """Send an OBS request and return the response data dict."""
        self._msg_id += 1
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": f"py_{self._msg_id}",
                "requestData": request_data or {},
            },
        }
        self._send_json(payload)
        response = self._recv_json()
        if response.get("op") == 7:
            resp_data = response.get("d", {})
            if resp_data.get("requestStatus", {}).get("result") is False:
                comment = resp_data.get("requestStatus", {}).get("comment", "unknown error")
                raise OBSWebSocketError(f"{request_type}: {comment}")
            return resp_data.get("responseData", {})
        raise OBSWebSocketError(f"Unexpected op {response.get('op')} for {request_type}")

    # -- WebSocket framing ----------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            buf += chunk
        return buf

    def _recv_frame(self) -> bytes:
        hdr = self._recv_exact(2)
        fin = (hdr[0] & 0x80) != 0
        opcode = hdr[0] & 0x0F
        masked = (hdr[1] & 0x80) != 0
        length = hdr[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)

        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == 0x9:  # ping
            self._send_frame(b"", opcode=0xA)  # pong
            return self._recv_frame()

        return payload

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        frame = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            frame += bytes([length])
        elif length < 65536:
            frame += bytes([126]) + struct.pack("!H", length)
        else:
            frame += bytes([127]) + struct.pack("!Q", length)
        frame += payload
        self._sock.sendall(frame)

    def _send_json(self, obj: dict) -> None:
        self._send_frame(json.dumps(obj).encode())

    def _recv_json(self) -> dict:
        return json.loads(self._recv_frame().decode())

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


# ---- OBS operations ---------------------------------------------------------

def cmd_start(client: OBSClient, args: argparse.Namespace) -> None:
    """Start NDI output and streaming."""
    # Ensure the NDI output is enabled in the NDI filter/output settings.
    # First, check if streaming is active
    status = client.call("GetStreamStatus")
    if status.get("outputActive"):
        print("NDI output already active")
        return

    # The NDI output is configured in the OBS profile.
    # OBS 28+ with DistroAV enables NDI output via the Tools menu,
    # and it persists as part of the profile.  If the profile has
    # NDI Main Output enabled, it starts automatically with OBS.
    # We just ensure streaming/virtual-cam are running.
    if not status.get("outputActive"):
        # Attempt to start streaming (some NDI plugin versions hook into streaming)
        try:
            client.call("StartStream")
            print("NDI output started (via Streaming)")
        except OBSWebSocketError as e:
            if "already" in str(e).lower():
                pass
            else:
                print(f"Note: {e}")
                print("If NDI output isn't active, enable it manually in OBS: Tools → NDI Output Settings")

    # Also start the virtual camera (so the host itself can use it)
    try:
        vcam_status = client.call("GetVirtualCamStatus")
        if not vcam_status.get("outputActive"):
            client.call("StartVirtualCam")
            print("Virtual camera started (/dev/video10)")
    except OBSWebSocketError:
        print("Virtual camera not available (v4l2loopback may not be loaded)")


def cmd_stop(client: OBSClient, args: argparse.Namespace) -> None:
    """Stop NDI output."""
    status = client.call("GetStreamStatus")
    if status.get("outputActive"):
        client.call("StopStream")
        print("NDI output stopped")
    else:
        print("NDI output not active")

    try:
        vcam_status = client.call("GetVirtualCamStatus")
        if vcam_status.get("outputActive"):
            client.call("StopVirtualCam")
            print("Virtual camera stopped")
    except OBSWebSocketError:
        pass


def cmd_status(client: OBSClient, args: argparse.Namespace) -> None:
    """Print OBS and NDI status."""
    version = client.call("GetVersion")
    stream = client.call("GetStreamStatus")
    scenes = client.call("GetSceneList")
    current = scenes.get("currentProgramSceneName", "unknown")

    print(f"OBS Version:    {version.get('obsVersion', 'unknown')}")
    print(f"WebSocket:      {version.get('obsWebSocketVersion', 'unknown')}")
    print(f"Streaming:      {'ACTIVE' if stream.get('outputActive') else 'inactive'}")
    print(f"Active Scene:   {current}")

    # Check NDI-specific settings via GetProfileParameter
    try:
        ndi_enabled = client.call("GetProfileParameter", {
            "parameterCategory": "NDI",
            "parameterName": "MainOutputEnabled",
        })
        ndi_name = client.call("GetProfileParameter", {
            "parameterCategory": "NDI",
            "parameterName": "MainOutputName",
        })
        print(f"NDI Output:     {'enabled' if ndi_enabled.get('parameterValue') == 'true' else 'disabled'}")
        print(f"NDI Name:       {ndi_name.get('parameterValue', 'unknown')}")
    except OBSWebSocketError:
        print("NDI Output:     unknown (plugin may not be loaded)")

    print(f"\nScenes ({len(scenes.get('scenes', []))}):")
    for s in scenes.get("scenes", []):
        marker = " ← current" if s["sceneName"] == current else ""
        print(f"  - {s['sceneName']}{marker}")


def cmd_scene(client: OBSClient, args: argparse.Namespace) -> None:
    """Switch to a named scene."""
    client.call("SetCurrentProgramScene", {"sceneName": args.scene_name})
    print(f"Switched to scene: {args.scene_name}")


def cmd_display(client: OBSClient, args: argparse.Namespace) -> None:
    """Switch the desktop capture source to a specific display."""
    display_num = args.display_num
    # Find the current desktop capture source
    scene_name = client.call("GetSceneList")["currentProgramSceneName"]
    items = client.call("GetSceneItemList", {"sceneName": scene_name})

    source_item_id = None
    for item in items.get("sceneItems", []):
        if "Desktop Capture" in item.get("sourceName", ""):
            source_item_id = item["sceneItemId"]
            break

    if source_item_id is None:
        print("No 'Desktop Capture' source found in current scene")
        return

    # The pipewire/xcomposite source uses "monitor" or "monitor_id" setting
    client.call("SetInputSettings", {
        "inputName": "Desktop Capture",
        "inputSettings": {"monitor": display_num},
    })
    print(f"Desktop Capture now targeting display {display_num}")


def cmd_window(client: OBSClient, args: argparse.Namespace) -> None:
    """Switch the capture source to a specific window by title substring."""
    window_title = args.window_title
    client.call("SetInputSettings", {
        "inputName": "Desktop Capture",
        "inputSettings": {
            "capture_window": window_title,
            "capture_mode": "window",
        },
    })
    print(f"Desktop Capture now targeting window: {window_title}")


# ---- CLI --------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Control OBS NDI screen sharing via obs-websocket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default=os.environ.get("OBS_WS_HOST", "localhost"),
                        help="OBS WebSocket host (default: localhost)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OBS_WS_PORT", "4455")),
                        help="OBS WebSocket port (default: 4455)")
    parser.add_argument("--password", default=os.environ.get("OBS_WS_PASSWORD", ""),
                        help="OBS WebSocket password (default: empty)")

    sub = parser.add_subparsers(dest="command", help="Commands")
    sub.add_parser("start", help="Start desktop capture and NDI output")
    sub.add_parser("stop", help="Stop NDI output")
    sub.add_parser("status", help="Show OBS/NDI state")

    scene_p = sub.add_parser("scene", help="Switch to a named scene")
    scene_p.add_argument("scene_name", help="Scene name to switch to")

    disp_p = sub.add_parser("display", help="Capture a specific display")
    disp_p.add_argument("display_num", type=int, help="Display number (0, 1, …)")

    win_p = sub.add_parser("window", help="Capture a specific window")
    win_p.add_argument("window_title", help="Window title substring to match")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        with OBSClient(args.host, args.port, args.password) as client:
            if args.command == "start":
                cmd_start(client, args)
            elif args.command == "stop":
                cmd_stop(client, args)
            elif args.command == "status":
                cmd_status(client, args)
            elif args.command == "scene":
                cmd_scene(client, args)
            elif args.command == "display":
                cmd_display(client, args)
            elif args.command == "window":
                cmd_window(client, args)
    except OBSWebSocketError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("\nMake sure OBS is running with WebSocket enabled:", file=sys.stderr)
        print("  obs --profile NDI_ScreenShare --collection NDI_ScreenShare", file=sys.stderr)
        print("  Then in OBS: Tools → obs-websocket Settings → Enable", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        print(f"ERROR: Cannot connect to OBS at {args.host}:{args.port}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
