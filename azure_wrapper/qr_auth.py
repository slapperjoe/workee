"""QrAuthManager — QR code authentication for Australian government compliance.

Replaces TOTP-based MFA with a scannable QR code login flow:

1. Generate a cryptographically random challenge token
2. Render it as a QR code on a local login page
3. Government-approved app scans the QR code
4. App sends back a signed auth token via callback endpoint
5. Validate the token (HMAC-SHA256 signature + expiry check)
6. Signal the session manager to proceed

Australian Government ISM (Information Security Manual) compliance:
- ISM-1736: Multi-factor authentication
- ISM-0990: Cryptographic key management — HMAC-SHA256
- ISM-1745: Session termination — tokens expire after 5 minutes
- ISM-1173: CSRF protection via unpredictable state parameter
- ISM-1401: Approved cryptographic algorithms (SHA-256, HMAC)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import qrcode
import qrcode.image.svg
from aiohttp import web

if TYPE_CHECKING:
    from asyncio import Task

logger = logging.getLogger(__name__)

# Default HMAC key size (bytes) — ISM-0990 requires >= 128-bit keys
DEFAULT_KEY_BYTES = 32  # 256 bits

# Token expiry (seconds)
DEFAULT_TOKEN_TTL = 300  # 5 minutes

# Challenge ID entropy (bytes)
CHALLENGE_ID_BYTES = 24

# State parameter entropy (bytes)
STATE_BYTES = 16


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class QrAuthConfig:
    """Configuration for QR code authentication.

    Attributes:
        signing_key: Base64-encoded HMAC key for token validation.
                     Generated fresh if not provided.
        token_ttl: Token lifetime in seconds (default 300 = 5 min).
        callback_host: Host for the local callback server (default 127.0.0.1).
        callback_port: Port for the local callback server (default 48480).
        callback_path: URL path for the token callback (default /callback).
        static_dir: Directory with static assets for the login page.
        page_title: Title shown on the QR login page.
        page_instructions: Instructions shown below the QR code.
    """

    signing_key: str = ""
    token_ttl: int = DEFAULT_TOKEN_TTL
    callback_host: str = "127.0.0.1"
    callback_port: int = 48480
    callback_path: str = "/callback"
    static_dir: str = ""
    page_title: str = "Sign in with your government identity app"
    page_instructions: str = (
        "Scan this QR code with your government-approved "
        "authentication app to sign in."
    )

    @property
    def signing_key_bytes(self) -> bytes:
        """Return the signing key as raw bytes.

        If signing_key is empty, generates a cryptographically random key
        and caches it for the lifetime of this config object.
        """
        if not hasattr(self, "_cached_signing_key"):
            if self.signing_key:
                try:
                    self._cached_signing_key = base64.b64decode(self.signing_key)
                except Exception:
                    self._cached_signing_key = secrets.token_bytes(DEFAULT_KEY_BYTES)
            else:
                self._cached_signing_key = secrets.token_bytes(DEFAULT_KEY_BYTES)
        return self._cached_signing_key


@dataclass
class QrAuthChallenge:
    """An active QR code authentication challenge.

    Attributes:
        challenge_id: Unique unpredictable challenge identifier.
        state: CSRF state token sent to the client.
        qr_data: The data encoded in the QR code (URL with challenge_id + state).
        signing_key: HMAC key for this challenge.
        created_at: Unix timestamp when the challenge was created.
        token_ttl: Token lifetime in seconds.
    """

    challenge_id: str
    state: str
    qr_data: str
    signing_key: bytes
    created_at: float
    token_ttl: int


@dataclass
class QrAuthResult:
    """Result of a QR code authentication attempt.

    Attributes:
        success: Whether authentication succeeded.
        auth_token: The validated auth token (only if success=True).
        reason: Human-readable description of the result.
        elapsed_seconds: Time spent waiting for the scan.
    """

    success: bool
    auth_token: str = ""
    reason: str = ""
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# QrAuthManager
# ---------------------------------------------------------------------------


class QrAuthManager:
    """Generate and validate QR code authentication challenges.

    The manager:
    1. Creates challenges with unpredictable state + challenge IDs
    2. Validates incoming auth tokens against the challenge
    3. Provides a signal/wait pattern for the caller

    Usage::

        manager = QrAuthManager(config)
        challenge = manager.generate_challenge()

        # Serve the QR code on a local page, wait for scan
        server = QrAuthServer(manager, config)
        await server.start(challenge)

        # The server calls manager.set_token() when a token arrives,
        # which sets the event the caller waits on.
    """

    def __init__(self, config: QrAuthConfig | None = None):
        self.config = config or QrAuthConfig()
        self._active_challenge: QrAuthChallenge | None = None
        self._auth_token: str | None = None
        self._result_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Challenge lifecycle
    # ------------------------------------------------------------------

    def generate_challenge(self) -> QrAuthChallenge:
        """Create a fresh authentication challenge.

        Generates a unique challenge ID, CSRF state, and QR code data.
        Stores the challenge as active so validate_token() can check
        incoming tokens against it.

        Returns:
            QrAuthChallenge with QR data ready to render.
        """
        challenge_id = secrets.token_urlsafe(CHALLENGE_ID_BYTES)
        state = secrets.token_urlsafe(STATE_BYTES)
        signing_key = self.config.signing_key_bytes

        qr_data = json.dumps(
            {
                "version": 1,
                "challenge_id": challenge_id,
                "state": state,
                "callback_url": self._callback_url(),
                "created": int(time.time()),
                "provider": "aus-gov-id",  # Australian government identity
            }
        )

        challenge = QrAuthChallenge(
            challenge_id=challenge_id,
            state=state,
            qr_data=qr_data,
            signing_key=signing_key,
            created_at=time.time(),
            token_ttl=self.config.token_ttl,
        )

        self._active_challenge = challenge
        self._auth_token = None
        self._result_event.clear()

        logger.info(
            "QR auth challenge created: id=%s, ttl=%ds",
            challenge_id,
            self.config.token_ttl,
        )
        return challenge

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_token(self, token_data: dict) -> tuple[bool, str]:
        """Validate an auth token received from the government app.

        Performs the following checks:
        1. Token structure (required fields present)
        2. Challenge ID matches active challenge
        3. State parameter matches (CSRF)
        4. Token has not expired
        5. HMAC-SHA256 signature is valid

        Args:
            token_data: Parsed JSON body from the callback request.

        Returns:
            (valid: bool, reason: str)
        """
        challenge = self._active_challenge
        if challenge is None:
            return False, "No active challenge"

        # --- Structure validation ---
        required = {"challenge_id", "state", "auth_token", "timestamp", "signature"}
        missing = required - set(token_data.keys())
        if missing:
            return False, f"Missing fields: {', '.join(sorted(missing))}"

        # --- Challenge ID match ---
        if not hmac.compare_digest(
            token_data["challenge_id"].encode(),
            challenge.challenge_id.encode(),
        ):
            return False, "Challenge ID mismatch"

        # --- State (CSRF) match ---
        if not hmac.compare_digest(
            token_data["state"].encode(),
            challenge.state.encode(),
        ):
            return False, "State mismatch — possible CSRF"

        # --- Expiry check ---
        try:
            token_ts = int(token_data["timestamp"])
        except (ValueError, TypeError):
            return False, "Invalid timestamp"
        age = time.time() - token_ts
        if age < 0:
            return False, "Token timestamp is in the future"
        if age > challenge.token_ttl:
            return False, f"Token expired ({age:.0f}s old, max {challenge.token_ttl}s)"

        # --- Signature validation (HMAC-SHA256) ---
        expected_sig = self._compute_signature(
            challenge.signing_key,
            token_data["auth_token"],
            token_data["challenge_id"],
            token_data["state"],
            token_data["timestamp"],
        )
        try:
            provided_sig = base64.b64decode(token_data["signature"])
        except Exception:
            return False, "Invalid signature encoding"

        if not hmac.compare_digest(expected_sig, provided_sig):
            return False, "Signature validation failed"

        return True, "Token validated successfully"

    @staticmethod
    def _compute_signature(
        key: bytes, auth_token: str, challenge_id: str, state: str, timestamp: str
    ) -> bytes:
        """Compute HMAC-SHA256 signature over token fields.

        ISM-0990 / ISM-1401 compliant: uses HMAC with SHA-256.
        """
        message = f"{auth_token}:{challenge_id}:{state}:{timestamp}".encode()
        return hmac.new(key, message, hashlib.sha256).digest()

    # ------------------------------------------------------------------
    # Signal / wait pattern
    # ------------------------------------------------------------------

    def set_token(self, token: str) -> None:
        """Store the validated token and signal waiters."""
        self._auth_token = token
        self._result_event.set()
        logger.info("QR auth token set — signalling callers")

    @property
    def auth_token(self) -> str | None:
        """The validated auth token, or None if not yet received."""
        return self._auth_token

    @property
    def result_event(self) -> asyncio.Event:
        """Event set when a validated token is received.

        Await this to wait for the government app to scan and respond.
        """
        return self._result_event

    @property
    def active_challenge(self) -> QrAuthChallenge | None:
        """The current challenge, if any."""
        return self._active_challenge

    def clear(self) -> None:
        """Clear the active challenge and token."""
        self._active_challenge = None
        self._auth_token = None
        self._result_event.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _callback_url(self) -> str:
        """Build the full callback URL."""
        host = self.config.callback_host
        port = self.config.callback_port
        path = self.config.callback_path
        return f"http://{host}:{port}{path}"

    # ------------------------------------------------------------------
    # QR code rendering
    # ------------------------------------------------------------------

    @staticmethod
    def render_qr_svg(qr_data: str, box_size: int = 10) -> str:
        """Render QR code data as an SVG string.

        Args:
            qr_data: The string to encode in the QR code.
            box_size: Pixel size of each QR module (default 10).

        Returns:
            SVG markup as a string (suitable for embedding in HTML).
        """
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(qr_data, image_factory=factory, box_size=box_size)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")

    @staticmethod
    def render_qr_png_base64(qr_data: str, box_size: int = 10) -> str:
        """Render QR code data as a base64-encoded PNG data URL.

        Args:
            qr_data: The string to encode in the QR code.
            box_size: Pixel size of each QR module (default 10).

        Returns:
            A data:image/png;base64,... string.
        """
        img = qrcode.make(qr_data, box_size=box_size)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# QrAuthServer — aiohttp-based local callback server
# ---------------------------------------------------------------------------

_QR_LOGIN_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Oxygen-Sans, Ubuntu, Cantarell, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh;
  }}
  .card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 40px;
    text-align: center;
    max-width: 420px;
    width: 100%;
  }}
  h1 {{ font-size: 20px; margin-bottom: 8px; color: #f0f6fc; }}
  .qr-container {{
    background: #ffffff;
    border-radius: 8px;
    padding: 16px;
    margin: 24px 0;
    display: inline-block;
  }}
  .qr-container svg, .qr-container img {{
    display: block; width: 256px; height: 256px;
  }}
  .instructions {{ color: #8b949e; font-size: 14px; line-height: 1.5; }}
  .challenge-id {{
    color: #484f58; font-size: 12px; margin-top: 20px;
    word-break: break-all;
  }}
  .spinner {{
    display: inline-block; width: 20px; height: 20px;
    border: 2px solid #30363d; border-top-color: #58a6ff;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    margin-right: 8px; vertical-align: middle;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .status {{ margin-top: 16px; font-size: 14px; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <p class="instructions">{instructions}</p>
  <div class="qr-container">
    {qr_image}
  </div>
  <p class="instructions">Do not refresh this page.</p>
  <div class="status" id="status">
    <span class="spinner"></span> Waiting for scan...
  </div>
  <div class="challenge-id" id="challenge-id">
    Session: {challenge_id}
  </div>
</div>
<script>
  // Poll for completion
  const challengeId = "{challenge_id}";
  const pollInterval = 2000;
  let attempts = 0;

  function checkStatus() {{
    attempts++;
    fetch("/status/" + encodeURIComponent(challengeId))
      .then(r => r.json())
      .then(data => {{
        if (data.status === "authenticated") {{
          document.getElementById("status").innerHTML =
            "&#10003; Authenticated! You may close this page.";
          document.getElementById("status").style.color = "#3fb950";
        }} else if (data.status === "expired") {{
          document.getElementById("status").innerHTML =
            "&#10007; Session expired. Please reload to try again.";
          document.getElementById("status").style.color = "#f85149";
        }} else {{
          setTimeout(checkStatus, pollInterval);
        }}
      }})
      .catch(() => setTimeout(checkStatus, pollInterval));
  }}

  setTimeout(checkStatus, pollInterval);
</script>
</body>
</html>
"""


class QrAuthServer:
    """Local aiohttp server that serves the QR login page and token callback.

    Lifecycle::

        server = QrAuthServer(manager, config)
        await server.start(challenge)

        # ... government app scans QR, POSTs token to /callback ...

        token = await server.wait_for_token(timeout=300)
        await server.stop()
    """

    def __init__(self, manager: QrAuthManager, config: QrAuthConfig | None = None):
        self.manager = manager
        self.config = config or QrAuthConfig()
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ready: asyncio.Event | None = None
        self._challenge: QrAuthChallenge | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, challenge: QrAuthChallenge | None = None) -> str:
        """Start the local server and return the login page URL.

        Args:
            challenge: Optional challenge. If omitted, generates a new one.

        Returns:
            The URL of the QR code login page.
        """
        if challenge is None:
            challenge = self.manager.generate_challenge()
        self._challenge = challenge
        self._ready = asyncio.Event()
        self._stopped = False

        self._app = web.Application()
        self._app.router.add_get("/", self._handle_login_page)
        self._app.router.add_post(
            self.config.callback_path, self._handle_callback
        )
        self._app.router.add_get(
            "/status/{challenge_id}", self._handle_status
        )
        # Serve QR code image as inline resource
        self._app.router.add_get("/qr.png", self._handle_qr_image)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self.config.callback_host,
            self.config.callback_port,
        )
        await self._site.start()
        self._ready.set()

        url = f"http://{self.config.callback_host}:{self.config.callback_port}/"
        logger.info("QR auth server listening on %s", url)
        return url

    async def stop(self) -> None:
        """Shut down the server."""
        self._stopped = True
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._app = None
        self._runner = None
        self._site = None
        logger.info("QR auth server stopped")

    @property
    def actual_port(self) -> int | None:
        """The port the server is actually listening on.

        Useful when callback_port=0 (OS-assigned port).
        """
        if self._site is not None:
            try:
                return self._site._server.sockets[0].getsockname()[1]
            except Exception:
                pass
        return self.config.callback_port if self.config.callback_port != 0 else None

    async def wait_for_token(self, timeout: float | None = None) -> str | None:
        """Wait for the government app to scan and return a token.

        Args:
            timeout: Maximum seconds to wait. None = use token TTL.

        Returns:
            The auth token string, or None on timeout.
        """
        if timeout is None:
            timeout = float(
                self._challenge.token_ttl if self._challenge else DEFAULT_TOKEN_TTL
            )
        try:
            await asyncio.wait_for(
                self.manager.result_event.wait(), timeout=timeout
            )
            token = self.manager.auth_token
            self.manager.result_event.clear()
            return token
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        """Serve the QR code login page."""
        challenge = self._challenge
        if challenge is None:
            return web.Response(
                text="No active challenge", status=400
            )

        # Check expiry
        age = time.time() - challenge.created_at
        if age > challenge.token_ttl:
            html = """\
<!DOCTYPE html><html><body>
<h1>Session Expired</h1><p>Please reload to generate a new QR code.</p>
</body></html>"""
            return web.Response(text=html, content_type="text/html", status=410)

        # Render QR code as inline PNG data URL
        qr_b64 = QrAuthManager.render_qr_png_base64(challenge.qr_data)

        page = _QR_LOGIN_PAGE_TEMPLATE.format(
            title=self.config.page_title,
            instructions=self.config.page_instructions,
            qr_image=f'<img src="{qr_b64}" width="256" height="256" alt="QR Code">',
            challenge_id=challenge.challenge_id[:16] + "...",
        )
        return web.Response(text=page, content_type="text/html")

    async def _handle_qr_image(self, request: web.Request) -> web.Response:
        """Serve QR code as a standalone PNG image."""
        challenge = self._challenge
        if challenge is None:
            return web.Response(status=404)

        img = qrcode.make(challenge.qr_data, box_size=10)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """Receive auth token from the government app.

        Expects JSON::

            {
                "challenge_id": "...",
                "state": "...",
                "auth_token": "...",
                "timestamp": 1234567890,
                "signature": "base64-hmac-sha256"
            }
        """
        challenge = self._challenge
        if challenge is None:
            return web.json_response(
                {"error": "no_active_challenge"}, status=400
            )

        age = time.time() - challenge.created_at
        if age > challenge.token_ttl:
            return web.json_response(
                {"error": "challenge_expired"}, status=410
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid_json"}, status=400
            )

        valid, reason = self.manager.validate_token(body)
        if not valid:
            logger.warning("QR auth token rejected: %s", reason)
            return web.json_response(
                {"error": "token_rejected", "reason": reason}, status=403
            )

        # Store token and signal waiters
        self.manager.set_token(body["auth_token"])
        return web.json_response({"status": "authenticated"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Polling endpoint: check whether authentication completed."""
        challenge_id = request.match_info.get("challenge_id", "")
        challenge = self._challenge

        if challenge is None:
            return web.json_response(
                {"status": "error", "reason": "no_challenge"}
            )

        if challenge.challenge_id != challenge_id:
            return web.json_response(
                {"status": "error", "reason": "challenge_mismatch"}
            )

        if self.manager.auth_token:
            return web.json_response({"status": "authenticated"})

        age = time.time() - challenge.created_at
        if age > challenge.token_ttl:
            return web.json_response({"status": "expired"})

        return web.json_response({"status": "waiting"})


# ---------------------------------------------------------------------------
# Convenience: full QR auth flow
# ---------------------------------------------------------------------------


async def run_qr_auth_flow(
    manager: QrAuthManager | None = None,
    config: QrAuthConfig | None = None,
    timeout: float | None = None,
) -> QrAuthResult:
    """Run the complete QR code authentication flow.

    Starts a local server, presents a QR code, waits for the government
    app to scan and respond, then returns the result.

    Args:
        manager: Optional pre-configured QrAuthManager.
        config: Optional QrAuthConfig (used if manager is None).
        timeout: Max wait time in seconds.

    Returns:
        QrAuthResult with success status and auth token.
    """
    if manager is None:
        manager = QrAuthManager(config)
    if config is None:
        config = manager.config

    start_time = time.monotonic()

    challenge = manager.generate_challenge()
    server = QrAuthServer(manager, config)

    try:
        login_url = await server.start(challenge)
        logger.info("QR login page: %s", login_url)

        # In a real deployment, this URL would be shown to the user
        # or opened in a browser. The login page auto-polls status.

        token = await server.wait_for_token(timeout=timeout)
        elapsed = time.monotonic() - start_time

        if token:
            return QrAuthResult(
                success=True,
                auth_token=token,
                reason="QR code scanned and token validated",
                elapsed_seconds=elapsed,
            )
        return QrAuthResult(
            success=False,
            reason="Timed out waiting for QR scan",
            elapsed_seconds=elapsed,
        )
    finally:
        await server.stop()
        manager.clear()
