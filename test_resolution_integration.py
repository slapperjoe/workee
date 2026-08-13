"""Integration tests for dynamic resolution across the full priority chain.

Covers the acceptance criteria from the task:
  - Common resolutions: 1024x768, 1280x720, 1920x1080, 3840x2160
  - User-configured custom resolution (via string, dict, env var)
  - Edge cases: missing auto-detect fallback, invalid overrides,
    resizing while session is active, env var priority
  - Integration points: AzureConfig, get_resolution(), session viewport

These are pure unit/integration tests — no browser or VM required.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Top-level imports (not inside classes — avoids self-binding issues)
from azure_wrapper.config import AzureConfig
from azure_wrapper.resolution import (
    FALLBACK,
    MIN_HEIGHT,
    MIN_WIDTH,
    PRESETS,
    _parse_wlr_randr,
    _parse_xdpyinfo,
    _parse_xrandr,
    detect_system_resolution,
    get_resolution,
    parse,
    validate_resolution,
)


# ---------------------------------------------------------------------------
# Common resolution tests — 1024x768, 1280x720, 1920x1080, 3840x2160
# ---------------------------------------------------------------------------

class TestCommonResolutions:
    """Verify that all common resolutions resolve correctly."""

    def test_1024x768_wxh_string(self):
        res = get_resolution(configured="1024x768", auto_detect=False)
        assert res == {"width": 1024, "height": 768}

    def test_1024x768_dict(self):
        res = get_resolution(
            configured={"width": 1024, "height": 768}, auto_detect=False
        )
        assert res == {"width": 1024, "height": 768}

    def test_1280x720_preset(self):
        res = get_resolution(configured="hd", auto_detect=False)
        assert res == {"width": 1280, "height": 720}

    def test_1280x720_wxh_string(self):
        res = get_resolution(configured="1280x720", auto_detect=False)
        assert res == {"width": 1280, "height": 720}

    def test_1920x1080_preset_desktop(self):
        res = get_resolution(configured="desktop", auto_detect=False)
        assert res == {"width": 1920, "height": 1080}

    def test_1920x1080_preset_fhd(self):
        res = get_resolution(configured="fhd", auto_detect=False)
        assert res == {"width": 1920, "height": 1080}

    def test_1920x1080_wxh_string(self):
        res = get_resolution(configured="1920x1080", auto_detect=False)
        assert res == {"width": 1920, "height": 1080}

    def test_3840x2160_preset(self):
        res = get_resolution(configured="4k", auto_detect=False)
        assert res == {"width": 3840, "height": 2160}

    def test_3840x2160_wxh_string(self):
        res = get_resolution(configured="3840x2160", auto_detect=False)
        assert res == {"width": 3840, "height": 2160}

    def test_3840x2160_dict(self):
        res = get_resolution(
            configured={"width": 3840, "height": 2160}, auto_detect=False
        )
        assert res == {"width": 3840, "height": 2160}

    def test_all_spec_resolutions_usable(self):
        """Every resolution from the task spec works."""
        specs = ["1024x768", "1280x720", "1920x1080", "3840x2160"]
        for spec in specs:
            res = get_resolution(configured=spec, auto_detect=False)
            assert res["width"] >= MIN_WIDTH
            assert res["height"] >= MIN_HEIGHT


# ---------------------------------------------------------------------------
# User-configured custom resolutions
# ---------------------------------------------------------------------------

class TestCustomResolutions:
    """User-specified custom resolutions not in presets."""

    def test_custom_wxh_1440x900(self):
        res = get_resolution(configured="1440x900", auto_detect=False)
        assert res == {"width": 1440, "height": 900}

    def test_custom_wxh_2560x1080_ultrawide(self):
        res = get_resolution(configured="2560x1080", auto_detect=False)
        assert res == {"width": 2560, "height": 1080}

    def test_custom_wxh_3440x1440_ultrawide(self):
        res = get_resolution(configured="3440x1440", auto_detect=False)
        assert res == {"width": 3440, "height": 1440}

    def test_custom_dict_1600x900(self):
        res = get_resolution(
            configured={"width": 1600, "height": 900}, auto_detect=False
        )
        assert res == {"width": 1600, "height": 900}

    def test_custom_dict_unusual_aspect(self):
        """Unusual but valid aspect ratio e.g. 1536x2048 (portrait iPad)."""
        res = get_resolution(
            configured={"width": 1536, "height": 2048}, auto_detect=False
        )
        assert res == {"width": 1536, "height": 2048}

    def test_parse_wxh_case_insensitive(self):
        """WxH parsing is case-insensitive."""
        assert parse("1920X1080") == {"width": 1920, "height": 1080}
        assert parse("1366X768") == {"width": 1366, "height": 768}

    def test_parse_wxh_with_unicode_multiplication_sign(self):
        """WxH parsing handles the Unicode × character."""
        assert parse("1920\u00d71080") == {"width": 1920, "height": 1080}


# ---------------------------------------------------------------------------
# AZURE_RESOLUTION environment variable integration
# ---------------------------------------------------------------------------

class TestEnvVarResolution:
    """AZURE_RESOLUTION env var flows through get_resolution() correctly."""

    def test_env_var_preset_takes_effect(self, monkeypatch):
        monkeypatch.setenv("AZURE_RESOLUTION", "laptop")
        res = get_resolution(auto_detect=False)
        assert res == {"width": 1366, "height": 768}

    def test_env_var_wxh_takes_effect(self, monkeypatch):
        monkeypatch.setenv("AZURE_RESOLUTION", "1280x800")
        res = get_resolution(auto_detect=False)
        assert res == {"width": 1280, "height": 800}

    def test_env_var_overridden_by_explicit_configured(self, monkeypatch):
        """Explicit `configured` beats env var."""
        monkeypatch.setenv("AZURE_RESOLUTION", "laptop")
        res = get_resolution(configured="4k", auto_detect=False)
        assert res == {"width": 3840, "height": 2160}

    def test_env_var_empty_ignored(self, monkeypatch):
        monkeypatch.setenv("AZURE_RESOLUTION", "")
        res = get_resolution(auto_detect=False)
        assert res == FALLBACK

    def test_env_var_auto_falls_through(self, monkeypatch):
        monkeypatch.setenv("AZURE_RESOLUTION", "auto")
        res = get_resolution(auto_detect=False)
        assert res == FALLBACK

    def test_env_var_not_set_falls_through(self, monkeypatch):
        monkeypatch.delenv("AZURE_RESOLUTION", raising=False)
        res = get_resolution(auto_detect=False)
        assert res == FALLBACK


# ---------------------------------------------------------------------------
# Priority chain verification
# ---------------------------------------------------------------------------

class TestPriorityChain:
    """Verify: configured > client_viewport > system_detect > fallback."""

    def test_configured_beats_client_viewport(self):
        res = get_resolution(
            configured="laptop",
            client_viewport={"width": 2560, "height": 1440},
            fallback={"width": 1920, "height": 1080},
            auto_detect=False,
        )
        assert res == {"width": 1366, "height": 768}

    def test_configured_beats_fallback(self):
        res = get_resolution(
            configured="hd",
            fallback={"width": 1920, "height": 1080},
            auto_detect=False,
        )
        assert res == {"width": 1280, "height": 720}

    def test_client_viewport_when_no_configured(self):
        res = get_resolution(
            configured=None,
            client_viewport={"width": 1600, "height": 900},
            auto_detect=False,
        )
        assert res == {"width": 1600, "height": 900}

    def test_client_viewport_zero_ignored(self):
        res = get_resolution(
            configured=None,
            client_viewport={"width": 0, "height": 0},
            auto_detect=False,
        )
        assert res == FALLBACK

    def test_client_viewport_missing_keys_ignored(self):
        res = get_resolution(
            configured=None,
            client_viewport={},
            auto_detect=False,
        )
        assert res == FALLBACK

    def test_system_detect_with_xrandr(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        xrandr_output = (
            "Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 8192 x 8192\n"
            "DP-1 connected primary 2560x1440+0+0\n"
            "   2560x1440     60.00*+\n"
        )

        with patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=["xrandr"], returncode=0, stdout=xrandr_output, stderr=""
            ),
        ):
            res = get_resolution(
                configured=None, client_viewport=None, auto_detect=True
            )
            assert res == {"width": 2560, "height": 1440}

    def test_system_detect_with_wlr_randr(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

        wlr_output = "DP-1: 3440x1440@60Hz\n  Mode: 3440x1440@60Hz\n"

        with patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=["wlr-randr"], returncode=0, stdout=wlr_output, stderr=""
            ),
        ):
            res = get_resolution(
                configured=None, client_viewport=None, auto_detect=True
            )
            assert res == {"width": 3440, "height": 1440}

    def test_system_detect_failure_falls_to_fallback(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            res = get_resolution(
                configured=None, client_viewport=None, auto_detect=True
            )
            assert res == FALLBACK

    def test_system_detect_no_display_vars(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        res = get_resolution(
            configured=None, client_viewport=None, auto_detect=True
        )
        assert res == FALLBACK

    def test_fallback_used_when_nothing_else(self):
        res = get_resolution(
            configured=None,
            client_viewport=None,
            fallback={"width": 1024, "height": 768},
            auto_detect=False,
        )
        assert res == {"width": 1024, "height": 768}

    def test_default_fallback_is_desktop(self):
        res = get_resolution(auto_detect=False)
        assert res == FALLBACK


# ---------------------------------------------------------------------------
# Edge cases: invalid user overrides
# ---------------------------------------------------------------------------

class TestInvalidOverrides:
    """Invalid user overrides fall through gracefully."""

    def test_garbage_string_falls_to_fallback(self):
        res = get_resolution(configured="not-a-resolution", auto_detect=False)
        assert res == FALLBACK

    def test_very_long_string_falls_to_fallback(self):
        res = get_resolution(configured="a" * 100, auto_detect=False)
        assert res == FALLBACK

    def test_negative_dimensions_clamped(self):
        res = get_resolution(
            configured={"width": -100, "height": -50}, auto_detect=False
        )
        assert res == {"width": 800, "height": 600}

    def test_string_dimensions_too_small_clamped(self):
        # The regex requires 3-5 digits, so the smallest parseable WxH
        # below MIN_HEIGHT would be e.g. "800x599" (3 digits each)
        res = get_resolution(configured="800x599", auto_detect=False)
        assert res == {"width": 800, "height": 600}

    def test_string_dimensions_too_large_clamped(self):
        res = get_resolution(configured="9000x5000", auto_detect=False)
        assert res == {"width": 7680, "height": 4320}

    def test_non_str_dict_types_handled(self):
        assert parse(42) is None  # type: ignore[arg-type]
        assert parse(3.14) is None  # type: ignore[arg-type]

    def test_partial_dict_uses_fallback_height(self):
        res = get_resolution(
            configured={"width": 1600}, auto_detect=False
        )
        assert res["width"] == 1600
        assert res["height"] == 1080  # FALLBACK height

    def test_non_numeric_dict_values_falls_to_fallback(self):
        res = get_resolution(
            configured={"width": "abc", "height": "def"}, auto_detect=False
        )
        assert res == FALLBACK


# ---------------------------------------------------------------------------
# Edge cases: resizing while session is active (client viewport updates)
# ---------------------------------------------------------------------------

class TestResizingWhileActive:
    """Simulate dashboard frontend reporting new viewport mid-session."""

    def test_smaller_window_update(self):
        res = get_resolution(configured="desktop", auto_detect=False)
        assert res == {"width": 1920, "height": 1080}
        # Then client reports new size:
        res2 = get_resolution(
            configured=None,
            client_viewport={"width": 1024, "height": 768},
            auto_detect=False,
        )
        assert res2 == {"width": 1024, "height": 768}

    def test_larger_window_update(self):
        res = get_resolution(
            configured=None,
            client_viewport={"width": 2560, "height": 1440},
            auto_detect=False,
        )
        assert res == {"width": 2560, "height": 1440}

    def test_configured_override_persists_through_resize(self):
        res = get_resolution(
            configured="hd",
            client_viewport={"width": 2560, "height": 1440},
            auto_detect=False,
        )
        assert res == {"width": 1280, "height": 720}

    def test_multiple_sequential_resizes(self):
        sizes = [
            {"width": 1200, "height": 800},
            {"width": 1600, "height": 900},
            {"width": 1920, "height": 1080},
        ]
        for size in sizes:
            res = get_resolution(
                configured=None, client_viewport=size, auto_detect=False
            )
            assert res == size

    def test_client_viewport_none_doesnt_crash(self):
        res = get_resolution(client_viewport=None, auto_detect=False)
        assert res == FALLBACK


# ---------------------------------------------------------------------------
# AzureConfig integration
# ---------------------------------------------------------------------------

class TestAzureConfigIntegration:
    """AzureConfig correctly integrates with resolution module."""

    def test_default_viewport_matches_fallback(self):
        c = AzureConfig(email="u@c.com", password="pw")
        assert c.viewport == FALLBACK

    def test_resolution_field_defaults_to_none(self):
        c = AzureConfig(email="u@c.com", password="pw")
        assert c.resolution is None

    def test_resolution_field_accepts_preset(self):
        c = AzureConfig(email="u@c.com", password="pw", resolution="laptop")
        assert c.resolution == "laptop"

    def test_resolution_field_accepts_dict(self):
        custom = {"width": 1600, "height": 900}
        c = AzureConfig(email="u@c.com", password="pw", resolution=custom)
        assert c.resolution == custom

    def test_from_env_reads_azure_resolution(self, monkeypatch):
        monkeypatch.setenv("AZURE_EMAIL", "u@c.com")
        monkeypatch.setenv("AZURE_PASSWORD", "pw")
        monkeypatch.setenv("AZURE_RESOLUTION", "hd")
        c = AzureConfig.from_env()
        assert c.resolution == "hd"

    def test_from_env_no_resolution_defaults_to_none(self, monkeypatch):
        monkeypatch.setenv("AZURE_EMAIL", "u@c.com")
        monkeypatch.setenv("AZURE_PASSWORD", "pw")
        monkeypatch.delenv("AZURE_RESOLUTION", raising=False)
        c = AzureConfig.from_env()
        assert c.resolution is None

    def test_viewport_and_resolution_independent(self):
        c = AzureConfig(
            email="u@c.com", password="pw", resolution="hd",
            viewport={"width": 1920, "height": 1080},
        )
        assert c.viewport == {"width": 1920, "height": 1080}
        assert c.resolution == "hd"

    def test_config_repr_doesnt_leak_password(self):
        c = AzureConfig(email="u@c.com", password="secret", resolution="4k")
        r = repr(c)
        assert "secret" not in r
        assert "redacted" in r


# ---------------------------------------------------------------------------
# Session viewport resolution integration
# ---------------------------------------------------------------------------

class TestSessionViewportResolution:
    """Viewport flow: config → get_resolution."""

    def test_session_uses_configured_resolution(self):
        c = AzureConfig(email="u@c.com", password="pw", resolution="hd")
        res = get_resolution(
            configured=c.resolution, fallback=c.viewport, auto_detect=False,
        )
        assert res == {"width": 1280, "height": 720}

    def test_session_uses_viewport_when_no_resolution(self):
        c = AzureConfig(email="u@c.com", password="pw", resolution=None)
        res = get_resolution(
            configured=c.resolution, fallback=c.viewport, auto_detect=False,
        )
        assert res == c.viewport

    def test_session_client_viewport_injection(self):
        c = AzureConfig(email="u@c.com", password="pw", resolution=None)
        client_vp = {"width": 1440, "height": 900}
        res = get_resolution(
            configured=c.resolution, client_viewport=client_vp,
            fallback=c.viewport, auto_detect=False,
        )
        assert res == client_vp

    def test_full_config_to_resolution_flow(self):
        """End-to-end: config → get_resolution resolves correctly."""
        c = AzureConfig(email="u@c.com", password="pw", resolution="4k")
        viewport = get_resolution(
            configured=c.resolution,
            client_viewport=getattr(c, "_client_viewport", None),
            fallback=c.viewport,
        )
        assert viewport == {"width": 3840, "height": 2160}


# ---------------------------------------------------------------------------
# PRESETS completeness and FALLBACK validation
# ---------------------------------------------------------------------------

class TestPresetsAndFallback:
    def test_all_presets_valid(self):
        for name, dims in PRESETS.items():
            assert dims["width"] >= MIN_WIDTH, f"{name} width too small"
            assert dims["height"] >= MIN_HEIGHT, f"{name} height too small"
            assert dims["width"] <= 7680, f"{name} width too large"
            assert dims["height"] <= 4320, f"{name} height too large"

    def test_preset_names_are_usable(self):
        for name in PRESETS:
            res = get_resolution(configured=name, auto_detect=False)
            assert res == dict(PRESETS[name])

    def test_fallback_is_a_valid_preset(self):
        assert FALLBACK in PRESETS.values()

    def test_desktop_and_fhd_are_same(self):
        assert PRESETS["desktop"] == PRESETS["fhd"]

    def test_all_named_presets_correct(self):
        expected = {
            "desktop": (1920, 1080),
            "laptop": (1366, 768),
            "hd": (1280, 720),
            "fhd": (1920, 1080),
            "qhd": (2560, 1440),
            "4k": (3840, 2160),
        }
        assert len(PRESETS) == len(expected)
        for name, (w, h) in expected.items():
            assert PRESETS[name] == {"width": w, "height": h}


# ---------------------------------------------------------------------------
# validate_resolution edge cases
# ---------------------------------------------------------------------------

class TestValidateResolutionEdgeCases:
    def test_max_boundary_string(self):
        assert validate_resolution("7680x4320") is None

    def test_min_boundary_string(self):
        assert validate_resolution("800x600") is None

    def test_just_below_minimum(self):
        """validate_resolution() enforces MIN_WIDTH/MIN_HEIGHT on WxH strings."""
        err = validate_resolution("799x599")
        assert err is not None
        assert "at least" in err.lower()

    def test_whitespace_handled(self):
        assert validate_resolution("  desktop  ") is None

    def test_dict_with_zero_dimensions(self):
        err = validate_resolution({"width": 0, "height": 0})
        assert err is not None

    def test_dict_with_float_dimensions(self):
        assert validate_resolution({"width": 1920.0, "height": 1080.0}) is None

    def test_dict_with_string_numbers(self):
        """String numbers in dict are successfully int-cast by validate_resolution."""
        assert validate_resolution({"width": "1920", "height": "1080"}) is None


# ---------------------------------------------------------------------------
# Cross-module import and export tests
# ---------------------------------------------------------------------------

class TestCrossModuleExports:
    def test_get_resolution_in_package_root(self):
        from azure_wrapper import get_resolution as gr
        res = gr(configured="hd", auto_detect=False)
        assert res == {"width": 1280, "height": 720}

    def test_parse_resolution_in_package_root(self):
        from azure_wrapper import parse_resolution
        assert parse_resolution("desktop") == {"width": 1920, "height": 1080}

    def test_presets_in_package_root(self):
        from azure_wrapper import RESOLUTION_PRESETS
        assert "desktop" in RESOLUTION_PRESETS
        assert "4k" in RESOLUTION_PRESETS

    def test_get_resolution_imported_in_avd_client(self):
        from avd_client import AVDClient
        assert AVDClient is not None

    def test_get_resolution_imported_in_avd_login(self):
        from avd_login import authenticate
        assert callable(authenticate)

    def test_get_resolution_imported_in_avd_session(self):
        from avd_session import AVDSessionManager
        assert AVDSessionManager is not None


# ---------------------------------------------------------------------------
# Detect system resolution — unit-level parsing tests
# ---------------------------------------------------------------------------

class TestSystemDetectionParsers:
    """Verify the xrandr/wlr-randr/xdpyinfo output parsers."""

    def test_parse_xrandr_with_multiple_outputs(self):
        output = (
            "Screen 0: minimum 8 x 8, current 3840 x 2160, maximum 32767 x 32767\n"
            "eDP-1 connected primary 3840x2160+0+0\n"
            "   3840x2160     60.00*+  59.94\n"
            "   1920x1080     60.00\n"
            "HDMI-1 disconnected\n"
        )
        result = _parse_xrandr(output)
        assert result == {"width": 3840, "height": 2160}

    def test_parse_xrandr_second_output_active(self):
        """First output has no '*', but second does."""
        output = (
            "DP-1 connected\n"
            "   1920x1080     60.00\n"
            "HDMI-1 connected primary\n"
            "   2560x1440     59.95*+\n"
        )
        result = _parse_xrandr(output)
        assert result == {"width": 2560, "height": 1440}

    def test_parse_wlr_randr_multiple_modes(self):
        output = (
            "eDP-1: 1920x1080@60Hz\n"
            "  Mode: 1920x1080@60Hz\n"
            "  Mode: 3840x2160@60Hz\n"
            "  Mode: 2560x1440@60Hz\n"
        )
        result = _parse_wlr_randr(output)
        # First Mode line matched
        assert result == {"width": 1920, "height": 1080}

    def test_parse_wlr_randr_no_mode(self):
        output = "DP-1: Unknown\n  Position: 0,0\n"
        result = _parse_wlr_randr(output)
        assert result is None

    def test_parse_xdpyinfo_multiline(self):
        output = (
            "name of display: :0\n"
            "version number: 11.0\n"
            "screen #0:\n"
            "  dimensions:    2560x1440 pixels (677x381 millimeters)\n"
            "  resolution:    96x96 dots per inch\n"
        )
        result = _parse_xdpyinfo(output)
        assert result == {"width": 2560, "height": 1440}

    def test_parse_xdpyinfo_no_dimensions(self):
        output = "screen #0:\n  no window manager\n"
        result = _parse_xdpyinfo(output)
        assert result is None

    def test_detect_system_resolution_no_env(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        result = detect_system_resolution()
        assert result is None

    def test_detect_system_resolution_xrandr_timeout(self, monkeypatch):
        """xrandr timeout → falls through to xdpyinfo if DISPLAY is set."""
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        call_count = [0]

        def fake_run(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.TimeoutExpired("xrandr", 3)
            # xdpyinfo succeeds
            return subprocess.CompletedProcess(
                args=["xdpyinfo"], returncode=0,
                stdout="dimensions:    1920x1080 pixels", stderr="",
            )

        with patch.object(subprocess, "run", side_effect=fake_run):
            result = detect_system_resolution()
            assert result == {"width": 1920, "height": 1080}


# ---------------------------------------------------------------------------
# Smoke test: run all resolution-related tests from existing suite too
# ---------------------------------------------------------------------------

class TestSmokeExistingResolutionTests:
    """Quick re-verification that existing tests still pass."""

    def test_clamp_normal(self):
        from azure_wrapper.resolution import clamp
        assert clamp(1920, 1080) == {"width": 1920, "height": 1080}

    def test_parse_none(self):
        assert parse(None) is None

    def test_parse_auto(self):
        assert parse("auto") is None

    def test_resolve_configured_priority(self):
        from azure_wrapper.resolution import resolve
        assert resolve(configured="desktop") == {"width": 1920, "height": 1080}
