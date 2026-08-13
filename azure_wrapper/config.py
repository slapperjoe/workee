"""AzureConfig dataclass — single source of truth for all session parameters.

Populates from environment variables or `.env` file via `from_env()`.
Legacy `AVD_*` variable names are supported for backward compatibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from azure_wrapper.resolution import FALLBACK as RESOLUTION_FALLBACK


EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

CHROMIUM_ARGS = [
    "--enable-features=SharedArrayBuffer",
    "--enable-features=CrossOriginOpenerPolicy",
    "--enable-features=VaapiVideoDecoder",
]


@dataclass
class AzureConfig:
    # -- Credentials (required) --
    email: str = ""
    password: str = ""

    # -- Backend selection --
    backend: Literal["avd", "portal"] = "avd"

    # -- MFA --
    mfa_method: Literal["totp", "manual", "auto", "qr"] = "auto"
    totp_secret: str | None = None
    mfa_timeout: float = 120.0
    mfa_post_submit_timeout: float = 60.0

    # -- QR code auth (Australian government compliance) --
    qr_signing_key: str = ""          # Base64 HMAC key; auto-generated if empty
    qr_token_ttl: int = 300           # Token lifetime in seconds (ISM-1745)
    qr_callback_host: str = "127.0.0.1"
    qr_callback_port: int = 48480
    qr_callback_path: str = "/callback"

    # -- Session persistence --
    persistence_mode: Literal["persistent_context", "storage_state"] = (
        "persistent_context"
    )
    user_data_dir: str = "./azure-browser-profile"
    storage_state_path: str = "./auth-state.json"

    # -- Browser --
    headed: bool = False
    chromium_args: list[str] = field(default_factory=lambda: CHROMIUM_ARGS.copy())
    user_agent: str = EDGE_UA
    viewport: dict = field(default_factory=lambda: dict(RESOLUTION_FALLBACK))
    resolution: str | dict | None = None  # "auto", "desktop", "1920x1080", or {w,h}

    # -- Session monitor --
    heartbeat_interval: float = 300.0
    reauth_timeout: float = 300.0
    session_poll_interval: float = 15.0

    # -- Portal-specific --
    subscription_id: str | None = None

    # -- Debug --
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "AzureConfig":
        """Load configuration from environment variables.

        First tries the new `AZURE_*` var names, then falls back to
        legacy `AVD_*` names. If a `.env` file exists in the given path
        or cwd, it is loaded first via python-dotenv.

        Args:
            env_path: Optional path to a .env file. Defaults to
                      `<cwd>/.env` if it exists.
        """
        # Load .env
        if env_path is None:
            env_path = os.path.join(os.getcwd(), ".env")
        env_f = Path(env_path)
        if env_f.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_f)
            except ImportError:
                pass

        def _get(key: str, default: str = "") -> str:
            return os.getenv(key, default).strip()

        # --- Credentials ---
        email = _get("AZURE_EMAIL") or _get("AVD_EMAIL")
        password = _get("AZURE_PASSWORD") or _get("AVD_PASSWORD")

        # --- Backend ---
        backend_raw = (
            _get("AZURE_BACKEND") or _get("AVD_BACKEND") or "avd"
        )
        backend = backend_raw if backend_raw in ("avd", "portal") else "avd"

        # --- MFA ---
        mfa_method_raw = (
            _get("AZURE_MFA_METHOD") or _get("AVD_MFA_METHOD") or "auto"
        )
        mfa_method = (
            mfa_method_raw
            if mfa_method_raw in ("totp", "manual", "auto", "qr")
            else "auto"
        )
        totp_secret = (
            _get("AZURE_TOTP_SECRET") or _get("AVD_TOTP_SECRET") or None
        )
        mfa_timeout = float(_get("AZURE_MFA_TIMEOUT") or _get("AVD_MFA_TIMEOUT") or "120")
        mfa_post_submit = float(
            _get("AZURE_MFA_POST_SUBMIT_TIMEOUT")
            or _get("AVD_MFA_POST_SUBMIT_TIMEOUT")
            or "60"
        )

        # --- QR code auth ---
        qr_signing_key = (
            _get("AZURE_QR_SIGNING_KEY")
            or _get("AVD_QR_SIGNING_KEY")
            or ""
        )
        qr_token_ttl = int(
            _get("AZURE_QR_TOKEN_TTL")
            or _get("AVD_QR_TOKEN_TTL")
            or "300"
        )
        qr_callback_host = (
            _get("AZURE_QR_CALLBACK_HOST")
            or _get("AVD_QR_CALLBACK_HOST")
            or "127.0.0.1"
        )
        qr_callback_port = int(
            _get("AZURE_QR_CALLBACK_PORT")
            or _get("AVD_QR_CALLBACK_PORT")
            or "48480"
        )
        qr_callback_path = (
            _get("AZURE_QR_CALLBACK_PATH")
            or _get("AVD_QR_CALLBACK_PATH")
            or "/callback"
        )

        # --- Persistence ---
        persistence_raw = _get("AZURE_PERSISTENCE") or "persistent_context"
        persistence_mode = (
            persistence_raw
            if persistence_raw in ("persistent_context", "storage_state")
            else "persistent_context"
        )
        user_data_dir = (
            _get("AZURE_USER_DATA_DIR") or "./azure-browser-profile"
        )
        storage_state = (
            _get("AZURE_STORAGE_STATE")
            or _get("AVD_STORAGE_STATE_PATH")
            or "./auth-state.json"
        )

        # --- Browser ---
        headed_raw = (
            _get("AZURE_HEADED")
            or _get("AVD_HEADED")
            or ""
        ).lower()
        headed = headed_raw in ("1", "true", "yes")

        resolution_raw = _get("AZURE_RESOLUTION") or None

        # --- Monitor ---
        heartbeat = float(_get("AZURE_HEARTBEAT") or "300")
        reauth_timeout = float(
            _get("AZURE_REAUTH_TIMEOUT")
            or _get("AVD_SESSION_REAUTH_TIMEOUT")
            or "300"
        )
        poll_interval = float(
            _get("AZURE_SESSION_POLL_INTERVAL")
            or _get("AVD_SESSION_POLL_INTERVAL")
            or "15"
        )

        # --- Portal ---
        subscription_id = _get("AZURE_SUBSCRIPTION_ID") or None

        # --- Logging ---
        log_level = _get("AZURE_LOG_LEVEL").upper() or "INFO"

        return cls(
            email=email,
            password=password,
            backend=backend,  # type: ignore[arg-type]
            mfa_method=mfa_method,  # type: ignore[arg-type]
            totp_secret=totp_secret or None,
            mfa_timeout=mfa_timeout,
            mfa_post_submit_timeout=mfa_post_submit,
            qr_signing_key=qr_signing_key,
            qr_token_ttl=qr_token_ttl,
            qr_callback_host=qr_callback_host,
            qr_callback_port=qr_callback_port,
            qr_callback_path=qr_callback_path,
            persistence_mode=persistence_mode,  # type: ignore[arg-type]
            user_data_dir=user_data_dir,
            storage_state_path=storage_state,
            headed=headed,
            resolution=resolution_raw,
            heartbeat_interval=heartbeat,
            reauth_timeout=reauth_timeout,
            session_poll_interval=poll_interval,
            subscription_id=subscription_id,
            log_level=log_level,
        )

    def __repr__(self) -> str:
        """Redact password in repr for security."""
        return (
            f"AzureConfig(email={self.email!r}, backend={self.backend!r}, "
            f"mfa_method={self.mfa_method!r}, headed={self.headed}, "
            f"password=<redacted>)"
        )
