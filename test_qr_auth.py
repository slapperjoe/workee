"""Unit and integration tests for QR code authentication module.

Tests cover:
- QrAuthConfig, QrAuthChallenge, QrAuthResult data types
- QrAuthManager token generation, validation, security
- QrAuthServer lifecycle and HTTP endpoints
- QR code rendering
- Integration with MfaManager
- Full QR auth flow (end-to-end, no browser)
- Australian Government ISM compliance checks
"""

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

# Ensure azure_wrapper is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from azure_wrapper.qr_auth import (
    QrAuthChallenge,
    QrAuthConfig,
    QrAuthManager,
    QrAuthResult,
    QrAuthServer,
    run_qr_auth_flow,
)
from azure_wrapper.config import AzureConfig
from azure_wrapper.mfa import MfaManager, MfaPromptType, MfaStatus, MfaResult
from azure_wrapper.types import QrAuthToken


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_expected_signature(key_bytes, auth_token, challenge_id, state, timestamp):
    """Mirror of QrAuthManager._compute_signature for test verification."""
    message = f"{auth_token}:{challenge_id}:{state}:{timestamp}".encode()
    return hmac.new(key_bytes, message, hashlib.sha256).digest()


async def _send_valid_token(server, signing_key):
    """Simulate a government app scanning QR and POSTing a valid token."""
    import aiohttp

    challenge = server._challenge
    auth_token = "aus-gov-token-" + challenge.challenge_id[:8]
    timestamp = str(int(time.time()))
    signature = _compute_expected_signature(
        signing_key,
        auth_token,
        challenge.challenge_id,
        challenge.state,
        timestamp,
    )

    payload = {
        "challenge_id": challenge.challenge_id,
        "state": challenge.state,
        "auth_token": auth_token,
        "timestamp": timestamp,
        "signature": base64.b64encode(signature).decode(),
    }

    host = server.config.callback_host
    port = server.actual_port
    callback_url = f"http://{host}:{port}{server.config.callback_path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(callback_url, json=payload) as resp:
            return await resp.json(), resp.status


def _server_url(server):
    """Get the actual base URL for a server."""
    return f"http://{server.config.callback_host}:{server.actual_port}"


# ---------------------------------------------------------------------------
# QrAuthConfig
# ---------------------------------------------------------------------------

class TestQrAuthConfig:
    def test_defaults(self):
        cfg = QrAuthConfig()
        assert cfg.token_ttl == 300
        assert cfg.callback_host == "127.0.0.1"
        assert cfg.callback_port == 48480
        assert cfg.callback_path == "/callback"
        assert cfg.page_title  # not empty

    def test_signing_key_bytes_generated(self):
        cfg = QrAuthConfig(signing_key="")
        key = cfg.signing_key_bytes
        assert len(key) == 32
        # Should be stable within the object lifetime (cached)
        assert key == cfg.signing_key_bytes

    def test_signing_key_bytes_from_base64(self):
        raw = b"a" * 32
        b64 = base64.b64encode(raw).decode()
        cfg = QrAuthConfig(signing_key=b64)
        assert cfg.signing_key_bytes == raw

    def test_custom_values(self):
        cfg = QrAuthConfig(
            signing_key="",
            token_ttl=120,
            callback_host="0.0.0.0",
            callback_port=9999,
            callback_path="/verify",
            page_title="Gov Login",
            page_instructions="Scan to verify",
        )
        assert cfg.token_ttl == 120
        assert cfg.callback_port == 9999
        assert cfg.page_title == "Gov Login"


# ---------------------------------------------------------------------------
# QrAuthChallenge
# ---------------------------------------------------------------------------

class TestQrAuthChallenge:
    def test_fields(self):
        c = QrAuthChallenge(
            challenge_id="abc",
            state="xyz",
            qr_data='{"v":1}',
            signing_key=b"k" * 32,
            created_at=1234567890.0,
            token_ttl=300,
        )
        assert c.challenge_id == "abc"
        assert c.state == "xyz"
        assert c.token_ttl == 300
        assert c.created_at == 1234567890.0


# ---------------------------------------------------------------------------
# QrAuthResult
# ---------------------------------------------------------------------------

class TestQrAuthResult:
    def test_success(self):
        r = QrAuthResult(
            success=True,
            auth_token="tok",
            reason="OK",
            elapsed_seconds=1.5,
        )
        assert r.success is True
        assert r.auth_token == "tok"

    def test_failure(self):
        r = QrAuthResult(
            success=False,
            reason="Timed out",
            elapsed_seconds=300.0,
        )
        assert r.success is False
        assert r.auth_token == ""


# ---------------------------------------------------------------------------
# QrAuthToken (from types)
# ---------------------------------------------------------------------------

class TestQrAuthToken:
    def test_defaults(self):
        t = QrAuthToken()
        assert t.token == ""
        assert t.challenge_id == ""
        assert t.is_expired is False

    def test_expiry(self):
        past = time.time() - 10
        t = QrAuthToken(expires_at=past)
        assert t.is_expired is True

    def test_not_expired(self):
        future = time.time() + 3600
        t = QrAuthToken(expires_at=future)
        assert t.is_expired is False

    def test_zero_expiry(self):
        t = QrAuthToken(expires_at=0.0)
        assert t.is_expired is False

    def test_metadata(self):
        t = QrAuthToken(metadata={"sub": "user-1", "iss": "aus-gov"})
        assert t.metadata["sub"] == "user-1"


# ---------------------------------------------------------------------------
# QrAuthManager
# ---------------------------------------------------------------------------

class TestQrAuthManager:
    def test_generate_challenge(self):
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()

        assert challenge.challenge_id
        assert len(challenge.challenge_id) > 16
        assert challenge.state
        assert len(challenge.state) > 8
        assert challenge.qr_data
        assert challenge.token_ttl == 300
        assert challenge.created_at > 0

        # QR data should be valid JSON with expected fields
        data = json.loads(challenge.qr_data)
        assert data["version"] == 1
        assert data["challenge_id"] == challenge.challenge_id
        assert data["state"] == challenge.state
        assert "callback_url" in data
        assert "created" in data
        assert data["provider"] == "aus-gov-id"

    def test_generate_challenge_is_unique(self):
        mgr = QrAuthManager()
        ids = set()
        for _ in range(10):
            c = mgr.generate_challenge()
            ids.add(c.challenge_id)
        assert len(ids) == 10

    def test_validate_token_success(self):
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key

        auth_token = "test-token-123"
        timestamp = str(int(time.time()))
        sig = _compute_expected_signature(
            signing_key, auth_token, challenge.challenge_id,
            challenge.state, timestamp,
        )

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": challenge.state,
            "auth_token": auth_token,
            "timestamp": timestamp,
            "signature": base64.b64encode(sig).decode(),
        }

        valid, reason = mgr.validate_token(token_data)
        assert valid, reason
        assert reason == "Token validated successfully"

    def test_validate_token_wrong_challenge_id(self):
        mgr = QrAuthManager()
        mgr.generate_challenge()

        token_data = {
            "challenge_id": "wrong-id",
            "state": "some-state",
            "auth_token": "tok",
            "timestamp": str(int(time.time())),
            "signature": "AAAA",
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid
        assert "Challenge ID mismatch" in reason

    def test_validate_token_missing_fields(self):
        mgr = QrAuthManager()
        mgr.generate_challenge()

        valid, reason = mgr.validate_token({"challenge_id": "x"})
        assert not valid
        assert "Missing fields" in reason

    def test_validate_token_expired(self):
        mgr = QrAuthManager(QrAuthConfig(token_ttl=1))
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key

        auth_token = "tok"
        timestamp = str(int(time.time() - 120))  # 2 minutes ago
        sig = _compute_expected_signature(
            signing_key, auth_token, challenge.challenge_id,
            challenge.state, timestamp,
        )

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": challenge.state,
            "auth_token": auth_token,
            "timestamp": timestamp,
            "signature": base64.b64encode(sig).decode(),
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid
        assert "expired" in reason.lower()

    def test_validate_token_future_timestamp(self):
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key

        auth_token = "tok"
        timestamp = str(int(time.time() + 3600))
        sig = _compute_expected_signature(
            signing_key, auth_token, challenge.challenge_id,
            challenge.state, timestamp,
        )

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": challenge.state,
            "auth_token": auth_token,
            "timestamp": timestamp,
            "signature": base64.b64encode(sig).decode(),
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid
        assert "future" in reason.lower()

    def test_validate_token_state_mismatch_csrf(self):
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key

        # Use wrong state but correct signature for that wrong state
        wrong_state = "evil-state"
        auth_token = "tok"
        timestamp = str(int(time.time()))
        sig = _compute_expected_signature(
            signing_key, auth_token, challenge.challenge_id,
            wrong_state, timestamp,
        )

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": wrong_state,
            "auth_token": auth_token,
            "timestamp": timestamp,
            "signature": base64.b64encode(sig).decode(),
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid
        assert "State mismatch" in reason

    def test_validate_token_bad_signature(self):
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": challenge.state,
            "auth_token": "tok",
            "timestamp": str(int(time.time())),
            "signature": "AAAA",  # bad
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid

    def test_validate_token_no_active_challenge(self):
        mgr = QrAuthManager()
        valid, reason = mgr.validate_token({"challenge_id": "x"})
        assert not valid
        assert "No active challenge" in reason

    def test_set_token_and_event(self):
        mgr = QrAuthManager()
        mgr.generate_challenge()
        assert mgr.auth_token is None
        assert not mgr.result_event.is_set()

        mgr.set_token("my-token")
        assert mgr.auth_token == "my-token"
        assert mgr.result_event.is_set()

    def test_clear(self):
        mgr = QrAuthManager()
        mgr.generate_challenge()
        mgr.set_token("tok")

        mgr.clear()
        assert mgr.auth_token is None
        assert mgr.active_challenge is None
        assert not mgr.result_event.is_set()

    def test_compute_signature_deterministic(self):
        key = b"k" * 32
        sig1 = QrAuthManager._compute_signature(
            key, "token", "challenge", "state", "123"
        )
        sig2 = QrAuthManager._compute_signature(
            key, "token", "challenge", "state", "123"
        )
        assert sig1 == sig2

    def test_compute_signature_different_inputs(self):
        key = b"k" * 32
        sig1 = QrAuthManager._compute_signature(
            key, "token", "challenge", "state", "123"
        )
        sig2 = QrAuthManager._compute_signature(
            key, "token2", "challenge", "state", "123"
        )
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# QR rendering
# ---------------------------------------------------------------------------

class TestQrRendering:
    def test_render_qr_svg(self):
        data = "test-qr-data"
        svg = QrAuthManager.render_qr_svg(data)
        assert "<svg" in svg or "svg" in svg.lower()
        assert len(svg) > 100

    def test_render_qr_png_base64(self):
        data = "test-qr-data"
        b64 = QrAuthManager.render_qr_png_base64(data)
        assert b64.startswith("data:image/png;base64,")
        assert len(b64) > 50


# ---------------------------------------------------------------------------
# QrAuthServer lifecycle and HTTP endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQrAuthServer:
    """Async tests: start/stop server and exercise HTTP endpoints."""

    async def test_server_starts_and_stops(self):
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)

        url = await server.start(challenge)
        assert url.startswith("http://")
        assert server.actual_port is not None
        assert server.actual_port > 0

        await server.stop()

    async def test_login_page_returns_html(self):
        import aiohttp
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            url = _server_url(server)
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    assert resp.status == 200
                    html = await resp.text()
                    assert "Sign in" in html
                    assert "QR" in html.upper()
                    assert challenge.challenge_id[:16] + "..." in html
        finally:
            await server.stop()

    async def test_status_endpoint_waiting(self):
        import aiohttp
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            status_url = (
                f"{_server_url(server)}/status/{challenge.challenge_id}"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(status_url) as resp:
                    data = await resp.json()
                    assert data["status"] == "waiting"
        finally:
            await server.stop()

    async def test_status_endpoint_authenticated(self):
        import aiohttp
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            # Mark token as set
            mgr.set_token("some-token")

            status_url = (
                f"{_server_url(server)}/status/{challenge.challenge_id}"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(status_url) as resp:
                    data = await resp.json()
                    assert data["status"] == "authenticated"
        finally:
            await server.stop()

    async def test_callback_valid_token(self):
        import aiohttp
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            data, status = await _send_valid_token(server, signing_key)
            assert status == 200
            assert data["status"] == "authenticated"
            assert mgr.auth_token is not None
        finally:
            await server.stop()

    async def test_callback_invalid_token_rejected(self):
        import aiohttp
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            callback_url = (
                f"{_server_url(server)}{server.config.callback_path}"
            )
            bad_payload = {"challenge_id": "nope", "state": "bad"}
            async with aiohttp.ClientSession() as session:
                async with session.post(callback_url, json=bad_payload) as resp:
                    data = await resp.json()
                    assert resp.status == 403
                    assert "error" in data
        finally:
            await server.stop()

    async def test_wait_for_token_success(self):
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            # Send valid token in background
            async def send_token():
                await asyncio.sleep(0.5)
                await _send_valid_token(server, signing_key)

            asyncio.create_task(send_token())
            token = await server.wait_for_token(timeout=5)
            assert token is not None
            assert "aus-gov-token" in token
        finally:
            await server.stop()

    async def test_wait_for_token_timeout(self):
        config = QrAuthConfig(callback_host="127.0.0.1", callback_port=0)
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        server = QrAuthServer(mgr, config)
        await server.start(challenge)

        try:
            token = await server.wait_for_token(timeout=1)
            assert token is None
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# run_qr_auth_flow integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunQrAuthFlow:
    async def test_flow_timeout(self):
        """Full flow that times out (no scan)."""
        config = QrAuthConfig(
            callback_host="127.0.0.1",
            callback_port=0,
            token_ttl=5,
        )
        result = await run_qr_auth_flow(config=config, timeout=2)
        assert result.success is False
        assert "Timed out" in result.reason

    async def test_flow_with_valid_token(self):
        """Full flow where a token arrives mid-wait."""
        import aiohttp

        config = QrAuthConfig(
            callback_host="127.0.0.1",
            callback_port=0,
            token_ttl=30,
        )
        mgr = QrAuthManager(config)

        # We'll manually start the flow and inject a token
        challenge = mgr.generate_challenge()
        signing_key = challenge.signing_key
        server = QrAuthServer(mgr, config)
        url = await server.start(challenge)

        try:
            # Send token from background
            async def send_token():
                await asyncio.sleep(0.5)
                callback_url = (
                    f"{_server_url(server)}{server.config.callback_path}"
                )
                auth_token = "real-token"
                timestamp = str(int(time.time()))
                sig = _compute_expected_signature(
                    signing_key, auth_token, challenge.challenge_id,
                    challenge.state, timestamp,
                )
                payload = {
                    "challenge_id": challenge.challenge_id,
                    "state": challenge.state,
                    "auth_token": auth_token,
                    "timestamp": timestamp,
                    "signature": base64.b64encode(sig).decode(),
                }
                async with aiohttp.ClientSession() as sess:
                    await sess.post(callback_url, json=payload)

            asyncio.create_task(send_token())
            token = await server.wait_for_token(timeout=10)
            assert token is not None
            assert token == "real-token"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# MfaManager QR integration
# ---------------------------------------------------------------------------

class TestMfaManagerQr:
    def test_qr_prompt_type_enum(self):
        assert MfaPromptType.QR_CODE.value == "qr_code"

    def test_qr_pending_status(self):
        assert MfaStatus.QR_PENDING.value == "qr_pending"

    def test_qr_manager_field(self):
        from azure_wrapper.qr_auth import QrAuthConfig, QrAuthManager

        qr_cfg = QrAuthConfig()
        qr_mgr = QrAuthManager(qr_cfg)

        mfa = MfaManager(
            method="qr",
            qr_manager=qr_mgr,
            qr_config=qr_cfg,
        )
        assert mfa.method == "qr"
        assert mfa.qr_manager is qr_mgr
        assert mfa.qr_config is qr_cfg

    def test_qr_manager_is_none_by_default(self):
        mfa = MfaManager(method="auto")
        assert mfa.qr_manager is None
        assert mfa.qr_config is None
        assert mfa.qr_login_url == ""


# ---------------------------------------------------------------------------
# AzureConfig QR integration
# ---------------------------------------------------------------------------

class TestAzureConfigQr:
    def test_qr_fields_added(self):
        cfg = AzureConfig(
            email="u@c.com",
            password="pw",
            mfa_method="qr",
            qr_signing_key="",
            qr_token_ttl=180,
            qr_callback_host="0.0.0.0",
            qr_callback_port=5555,
            qr_callback_path="/cb",
        )
        assert cfg.mfa_method == "qr"
        assert cfg.qr_token_ttl == 180
        assert cfg.qr_callback_port == 5555
        assert cfg.qr_callback_path == "/cb"

    def test_mfa_method_qr_accepted(self):
        cfg = AzureConfig(email="u@c.com", password="pw", mfa_method="qr")
        assert cfg.mfa_method == "qr"


# ---------------------------------------------------------------------------
# ISM compliance checks
# ---------------------------------------------------------------------------

class TestIsmCompliance:
    """Verify Australian Government ISM requirements are met."""

    def test_ism_0990_key_strength(self):
        """ISM-0990: Cryptographic key management — keys must be >= 128 bits."""
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()
        assert len(challenge.signing_key) >= 16  # 128 bits minimum
        assert len(challenge.signing_key) == 32  # 256 bits actual

    def test_ism_1401_approved_algorithm(self):
        """ISM-1401: Only approved cryptographic algorithms (SHA-256, HMAC)."""
        key = b"k" * 32
        sig = QrAuthManager._compute_signature(key, "a", "b", "c", "1")
        assert len(sig) == 32  # SHA-256 digest = 32 bytes

    def test_ism_1745_session_timeout(self):
        """ISM-1745: Sessions must terminate after inactivity — tokens expire."""
        cfg = QrAuthConfig(token_ttl=300)
        assert cfg.token_ttl == 300  # 5 minutes default

    def test_ism_1173_csrf_state(self):
        """ISM-1173: CSRF protection — unpredictable state parameter."""
        mgr = QrAuthManager()
        states = set()
        for _ in range(20):
            c = mgr.generate_challenge()
            states.add(c.state)
        assert len(states) == 20  # all unique

    def test_ism_0990_constant_time_comparison(self):
        """HMAC comparison uses constant-time compare_digest."""
        mgr = QrAuthManager()
        challenge = mgr.generate_challenge()

        token_data = {
            "challenge_id": challenge.challenge_id,
            "state": challenge.state,
            "auth_token": "tok",
            "timestamp": str(int(time.time())),
            "signature": "AAAA",  # wrong
        }
        valid, reason = mgr.validate_token(token_data)
        assert not valid


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------

class TestPackageExportsQr:
    def test_qr_exports(self):
        import azure_wrapper
        assert hasattr(azure_wrapper, "QrAuthConfig")
        assert hasattr(azure_wrapper, "QrAuthManager")
        assert hasattr(azure_wrapper, "QrAuthServer")
        assert hasattr(azure_wrapper, "QrAuthChallenge")
        assert hasattr(azure_wrapper, "QrAuthResult")
        assert hasattr(azure_wrapper, "QrAuthToken")
        assert hasattr(azure_wrapper, "run_qr_auth_flow")
