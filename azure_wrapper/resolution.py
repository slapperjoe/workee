"""Resolution resolver — dynamic viewport dimensions for browser sessions.

Supports:
  - Explicit {width, height} dicts.
  - Named presets: "desktop" (1920x1080), "laptop" (1366x768), "hd" (1280x720).
  - "auto" — resolved from an explicit override or the config/env default.
  - Environment variable AZURE_RESOLUTION.
  - Runtime override via /api/resolution from the dashboard frontend.
  - System-level auto-detection via DISPLAY/WAYLAND_DISPLAY + xrandr/wlr-randr.
  - get_resolution() convenience API for one-shot resolution with full priority chain.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Canonical resolution presets.
PRESETS: dict[str, dict[str, int]] = {
    "desktop": {"width": 1920, "height": 1080},
    "laptop": {"width": 1366, "height": 768},
    "hd": {"width": 1280, "height": 720},
    "fhd": {"width": 1920, "height": 1080},
    "qhd": {"width": 2560, "height": 1440},
    "4k": {"width": 3840, "height": 2160},
}

# The factory default when nothing else is configured.
FALLBACK = PRESETS["desktop"]

# Minimum supported dimensions (avoiding tiny / broken layouts).
MIN_WIDTH = 800
MIN_HEIGHT = 600

# Recognised resolution format: "WxH" (e.g. "1920x1080").
_RES_RE = re.compile(r"^(\d{3,5})[xX\u00d7](\d{3,5})$")


def clamp(width: int, height: int) -> dict[str, int]:
    """Clamp dimensions to safe minima and maxima."""
    w = max(MIN_WIDTH, min(width, 7680))
    h = max(MIN_HEIGHT, min(height, 4320))
    return {"width": w, "height": h}


def parse(raw: str | dict | None) -> dict[str, int] | None:
    """Parse a resolution spec into a {width, height} dict.

    Accepts:
      - None → returns None (caller should use fallback).
      - dict → validated and clamped.
      - str → preset name ("desktop"), WxH string ("1920x1080"),
              or "auto" (returns None).
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        w = raw.get("width", FALLBACK["width"])
        h = raw.get("height", FALLBACK["height"])
        try:
            return clamp(int(w), int(h))
        except (ValueError, TypeError):
            logger.warning("Invalid resolution dict: %r", raw)
            return None

    if isinstance(raw, str):
        spec = raw.strip().lower()
        if not spec or spec == "auto":
            return None  # caller resolves from context
        if spec in PRESETS:
            return dict(PRESETS[spec])
        # Try WxH format: "1920x1080", "1920X1080", "1920×1080"
        m = _RES_RE.match(spec)
        if m:
            try:
                return clamp(int(m.group(1)), int(m.group(2)))
            except (ValueError, IndexError):
                logger.warning("Invalid WxH string: %r", raw)
                return None
        logger.warning("Unrecognised resolution spec: %r", raw)
        return None

    logger.warning("Unsupported resolution type: %r", type(raw))
    return None


def resolve(
    configured: str | dict | None = None,
    client_viewport: dict[str, int] | None = None,
    fallback: dict[str, int] | None = None,
) -> dict[str, int]:
    """Resolve the final viewport dimensions.

    Priority (highest first):
      1. configured — explicit user setting (env var, config file, CLI flag).
      2. client_viewport — passed from the browser frontend at runtime.
      3. fallback — caller-supplied default (e.g. FALLBACK).

    Returns a clamped {width, height} dict.
    """
    fb = fallback or FALLBACK

    # 1. Try configured value.
    parsed = parse(configured)
    if parsed is not None:
        logger.debug("Using configured resolution: %s", parsed)
        return parsed

    # 2. Try client viewport (from browser).
    if client_viewport:
        w = client_viewport.get("width", 0)
        h = client_viewport.get("height", 0)
        if w > 0 and h > 0:
            resolved = clamp(w, h)
            logger.debug("Using client-reported viewport: %s", resolved)
            return resolved

    # 3. Fallback.
    logger.debug("Using fallback resolution: %s", fb)
    return dict(fb)


# ---------------------------------------------------------------------------
# System-level detection
# ---------------------------------------------------------------------------


def detect_system_resolution() -> dict[str, int] | None:
    """Auto-detect the current display resolution from the operating system.

    Priority:
      1. WAYLAND_DISPLAY env → try `wlr-randr` (wlroots compositors).
      2. DISPLAY env → try `xrandr` (X11).
      3. WAYLAND_DISPLAY env → try `xdpyinfo` (XWayland fallback).
      4. None of the above → return None (caller must use fallback).

    Returns:
        A {width, height} dict on success, or None if detection fails.
    """
    wayland = os.getenv("WAYLAND_DISPLAY")
    display = os.getenv("DISPLAY")

    # 1. Wayland native
    if wayland:
        try:
            result = subprocess.run(
                ["wlr-randr"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                parsed = _parse_wlr_randr(result.stdout)
                if parsed:
                    logger.debug("Detected via wlr-randr: %s", parsed)
                    return parsed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 2. X11 (native or XWayland)
    if display:
        try:
            result = subprocess.run(
                ["xrandr", "--current"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                parsed = _parse_xrandr(result.stdout)
                if parsed:
                    logger.debug("Detected via xrandr: %s", parsed)
                    return parsed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # 3. Xdpyinfo fallback
        try:
            result = subprocess.run(
                ["xdpyinfo"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                parsed = _parse_xdpyinfo(result.stdout)
                if parsed:
                    logger.debug("Detected via xdpyinfo: %s", parsed)
                    return parsed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    logger.debug("System resolution auto-detection not available")
    return None


def _parse_xrandr(output: str) -> dict[str, int] | None:
    """Parse xrandr output for the current resolution.

    Looks for the first connected output with a '*' (current mode marker).
    """
    for line in output.splitlines():
        # e.g. "   1920x1080     60.00*+  59.94    50.00"
        if "*" not in line:
            continue
        m = re.search(r"(\d{3,5})[xX\u00d7](\d{3,5})", line)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= MIN_WIDTH and h >= MIN_HEIGHT:
                return {"width": w, "height": h}
    return None


def _parse_wlr_randr(output: str) -> dict[str, int] | None:
    """Parse wlr-randr output for the current mode.

    Looks for lines like '  Mode: 1920x1080@60Hz'.
    """
    for line in output.splitlines():
        m = re.search(r"Mode:\s*(\d{3,5})[xX\u00d7](\d{3,5})", line)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= MIN_WIDTH and h >= MIN_HEIGHT:
                return {"width": w, "height": h}
    return None


def _parse_xdpyinfo(output: str) -> dict[str, int] | None:
    """Parse xdpyinfo output for screen dimensions.

    Looks for 'dimensions:    1920x1080 pixels'.
    """
    for line in output.splitlines():
        m = re.search(r"dimensions:\s+(\d{3,5})[xX\u00d7](\d{3,5})", line)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= MIN_WIDTH and h >= MIN_HEIGHT:
                return {"width": w, "height": h}
    return None


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


def get_resolution(
    configured: str | dict | None = None,
    client_viewport: dict[str, int] | None = None,
    fallback: dict[str, int] | None = None,
    auto_detect: bool = True,
) -> dict[str, int]:
    """Get the final viewport resolution with full priority chain.

    This is the primary public API. Resolution sources are consulted in
    priority order (highest first):

      1. *configured* — explicit user override (string, dict, or env var).
      2. *client_viewport* — browser-reported viewport dimensions.
      3. *system auto-detection* — OS display resolution (xrandr, etc.).
      4. *fallback* — caller-supplied or built-in FALLBACK.

    Args:
        configured: Explicit resolution — a preset name, "WxH" string, dict,
                    or None. If None, the AZURE_RESOLUTION env var is checked.
        client_viewport: A {width, height} dict from the browser frontend.
        fallback: Final fallback dict. Defaults to FALLBACK (1920x1080).
        auto_detect: When True (default), attempt system-level detection
                     when no explicit resolution or client viewport is set.

    Returns:
        A clamped {width, height} dict.

    Example:
        >>> get_resolution()                      # auto-detect → fallback
        {'width': 1920, 'height': 1080}
        >>> get_resolution(configured="laptop")   # preset
        {'width': 1366, 'height': 768}
        >>> get_resolution(configured="1280x800") # WxH string
        {'width': 1280, 'height': 800}
    """
    fb = fallback or FALLBACK

    # 1. Explicit configuration
    resolved_configured = configured
    if resolved_configured is None:
        # Check environment variable
        resolved_configured = os.getenv("AZURE_RESOLUTION")

    parsed = parse(resolved_configured)
    if parsed is not None:
        logger.debug("get_resolution: using configured resolution: %s", parsed)
        return parsed

    # 2. Client viewport
    if client_viewport:
        w = client_viewport.get("width", 0)
        h = client_viewport.get("height", 0)
        if w > 0 and h > 0:
            resolved = clamp(w, h)
            logger.debug("get_resolution: using client viewport: %s", resolved)
            return resolved

    # 3. System auto-detection
    if auto_detect:
        detected = detect_system_resolution()
        if detected is not None:
            logger.debug("get_resolution: using system detected: %s", detected)
            return detected

    # 4. Fallback
    logger.debug("get_resolution: using fallback: %s", fb)
    return dict(fb)


def validate_resolution(spec: str | dict) -> str | None:
    """Validate a resolution specification and return an error message.

    Returns None if valid, or a string describing the problem.
    Does NOT accept None or "auto" — those are valid *states*, not
    valid *resolutions*.

    Args:
        spec: A preset name (e.g. "desktop"), "WxH" string,
              or {width, height} dict.

    Returns:
        None if the spec is a valid resolution, or an error string.
    """
    if spec is None:
        return "Resolution spec must not be None"

    if isinstance(spec, dict):
        w = spec.get("width")
        h = spec.get("height")
        if w is None or h is None:
            return "Dict must contain 'width' and 'height' keys"
        try:
            w_int, h_int = int(w), int(h)
        except (ValueError, TypeError):
            return f"Width/height must be integers, got {type(w).__name__}/{type(h).__name__}"
        if w_int < MIN_WIDTH or h_int < MIN_HEIGHT:
            return f"Dimensions must be at least {MIN_WIDTH}x{MIN_HEIGHT}"
        if w_int > 7680 or h_int > 4320:
            return "Dimensions must not exceed 7680x4320"
        return None

    if isinstance(spec, str):
        stripped = spec.strip()
        if not stripped:
            return "Resolution string must not be empty"
        if stripped.lower() == "auto":
            return "'auto' is not a concrete resolution — use None instead"
        if stripped.lower() in PRESETS:
            return None
        if _RES_RE.match(stripped):
            # Also enforce dimension bounds
            m = _RES_RE.match(stripped)
            assert m is not None  # guaranteed by the if-check above
            w_int, h_int = int(m.group(1)), int(m.group(2))
            if w_int < MIN_WIDTH or h_int < MIN_HEIGHT:
                return f"Dimensions must be at least {MIN_WIDTH}x{MIN_HEIGHT}"
            if w_int > 7680 or h_int > 4320:
                return "Dimensions must not exceed 7680x4320"
            return None
        return f"Unrecognised resolution format: {stripped!r}"

    return f"Unsupported type: {type(spec).__name__}"
