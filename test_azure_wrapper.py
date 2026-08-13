"""Unit tests for azure_wrapper package (no browser required)."""

import sys
from pathlib import Path

# Ensure azure_wrapper is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import azure_wrapper
from azure_wrapper.config import AzureConfig, CHROMIUM_ARGS, EDGE_UA
from azure_wrapper.types import (
    VmInfo,
    VmConnection,
    SessionState,
    Credentials,
)
from azure_wrapper.mfa import MfaManager, MfaPromptType, MfaStatus, MfaResult
from azure_wrapper.monitor import SessionMonitor
from azure_wrapper.avd import AVDSession
from azure_wrapper.portal import PortalSession


class TestAzureConfig:
    def test_defaults(self):
        c = AzureConfig(email="u@c.com", password="pw")
        assert c.email == "u@c.com"
        assert c.password == "pw"
        assert c.backend == "avd"
        assert c.mfa_method == "auto"
        assert c.persistence_mode == "persistent_context"
        assert c.headed is False
        assert "SharedArrayBuffer" in c.chromium_args[0]

    def test_repr_redacts_password(self):
        c = AzureConfig(email="u@c.com", password="secret123")
        r = repr(c)
        assert "u@c.com" in r
        assert "secret123" not in r
        assert "redacted" in r

    def test_backend_override(self):
        c = AzureConfig(email="u@c.com", password="pw", backend="portal")
        assert c.backend == "portal"

    def test_mfa_config(self):
        c = AzureConfig(
            email="u@c.com",
            password="pw",
            mfa_method="totp",
            totp_secret="JBSWY3DPEHPK3PXP",
            mfa_timeout=60.0,
        )
        assert c.mfa_method == "totp"
        assert c.totp_secret == "JBSWY3DPEHPK3PXP"
        assert c.mfa_timeout == 60.0

    def test_persistence_mode_storage_state(self):
        c = AzureConfig(
            email="u@c.com",
            password="pw",
            persistence_mode="storage_state",
            storage_state_path="/tmp/auth.json",
        )
        assert c.persistence_mode == "storage_state"
        assert c.storage_state_path == "/tmp/auth.json"


class TestTypes:
    def test_vm_info_defaults(self):
        v = VmInfo()
        assert v.name == ""
        assert v.backend == ""

    def test_vm_info_full(self):
        v = VmInfo(
            id="vm-123",
            name="My VM",
            backend="portal",
            kind="virtualMachine",
            location="eastus",
            power_state="running",
        )
        assert v.id == "vm-123"
        assert v.location == "eastus"
        assert v.power_state == "running"

    def test_vm_connection(self):
        vc = VmConnection(
            vm_id="vm-1",
            vm_name="My VM",
            backend="avd",
            method="avd_rdp",
        )
        assert vc.is_connected is False  # no page
        assert vc.vm_name == "My VM"

    def test_session_state_defaults(self):
        s = SessionState()
        assert s.authenticated is False
        assert s.vm_count == 0
        assert s.mfa_pending is False

    def test_credentials(self):
        c = Credentials(email="u@c.com", password="pw")
        assert c.email == "u@c.com"


class TestMfaManagerInit:
    def test_default_init(self):
        m = MfaManager(method="auto")
        assert m.method == "auto"
        assert m.totp_secret is None
        assert m.timeout == 120.0
        assert m.mfa_pending is False
        assert m.mfa_event is not None

    def test_totp_init(self):
        m = MfaManager(method="totp", totp_secret="SECRET", timeout=60.0)
        assert m.method == "totp"
        assert m.totp_secret == "SECRET"
        assert m.timeout == 60.0

    def test_mfa_event_is_asyncio_event(self):
        import asyncio
        m = MfaManager()
        assert isinstance(m.mfa_event, asyncio.Event)

    def test_provide_code(self):
        import asyncio
        m = MfaManager(method="manual")
        assert m._pending_code is None
        m.provide_code("123456")
        assert m._pending_code == "123456"
        assert m.mfa_event.is_set()

    def test_provide_approval(self):
        m = MfaManager()
        m.provide_approval()
        assert m.mfa_event.is_set()


class TestMfaResult:
    def test_success(self):
        r = MfaResult(
            success=True,
            status=MfaStatus.SUCCESS,
            reason="TOTP submitted",
            method_used="totp",
            elapsed_seconds=2.5,
        )
        assert r.success is True
        assert r.status == MfaStatus.SUCCESS
        assert r.method_used == "totp"
        assert r.elapsed_seconds == 2.5

    def test_timeout(self):
        r = MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="Timed out",
        )
        assert r.success is False
        assert r.status == MfaStatus.TIMEOUT


class TestMfaPromptType:
    def test_enum_values(self):
        assert MfaPromptType.CODE_INPUT.value == "code_input"
        assert MfaPromptType.PUSH_PENDING.value == "push_pending"
        assert MfaPromptType.DEVICE_SELECT.value == "device_select"
        assert MfaPromptType.VERIFY_IDENTITY.value == "verify_identity"


class TestSessionMonitor:
    def test_init_defaults(self):
        sm = SessionMonitor()
        assert sm.poll_interval == 15.0
        assert sm.heartbeat_interval == 300.0
        assert sm.reauth_timeout == 300.0
        assert sm._running is False

    def test_init_custom(self):
        sm = SessionMonitor(
            poll_interval=10.0,
            heartbeat_interval=60.0,
            reauth_timeout=120.0,
        )
        assert sm.poll_interval == 10.0
        assert sm.heartbeat_interval == 60.0
        assert sm.reauth_count == 0

    def test_stop(self):
        sm = SessionMonitor()
        sm.stop()
        assert sm._stop_event.is_set()


class TestAVDSession:
    def test_init_sets_backend(self):
        from azure_wrapper.config import AzureConfig
        config = AzureConfig(email="u@c.com", password="pw", backend="avd")
        s = AVDSession(config)
        assert s.config.backend == "avd"

    def test_init_overrides_portal(self):
        from azure_wrapper.config import AzureConfig
        config = AzureConfig(email="u@c.com", password="pw", backend="portal")
        s = AVDSession(config)
        assert s.config.backend == "avd"

    def test_parse_feed_standard(self):
        data = {
            "value": [
                {
                    "workspace": {"name": "My WS"},
                    "resources": [
                        {
                            "name": "Session Desktop",
                            "resourceType": "Desktop",
                            "resourceId": "guid-123",
                        },
                    ],
                },
            ],
        }
        vms = AVDSession._parse_feed_response(data)
        assert len(vms) == 1
        assert vms[0].name == "Session Desktop"
        assert vms[0].kind == "Desktop"
        assert vms[0].workspace == "My WS"
        assert vms[0].backend == "avd"

    def test_parse_feed_empty(self):
        assert AVDSession._parse_feed_response({"value": []}) == []
        assert AVDSession._parse_feed_response([]) == []

    def test_parse_feed_flat_list(self):
        data = [{"name": "VM-1", "resourceType": "Desktop", "resourceId": "g1"}]
        vms = AVDSession._parse_feed_response(data)
        assert len(vms) == 1
        assert vms[0].name == "VM-1"

    def test_parse_feed_missing_keys(self):
        data = {"value": [{"workspace": {}, "resources": [{"name": "Bare"}]}]}
        vms = AVDSession._parse_feed_response(data)
        assert len(vms) == 1
        assert vms[0].name == "Bare"
        assert vms[0].kind == "unknown"
        assert vms[0].workspace == "Unknown"

    def test_is_guid(self):
        assert AVDSession._is_guid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is True
        assert AVDSession._is_guid("not-a-guid") is False
        assert AVDSession._is_guid("") is False

    def test_extract_guid(self):
        g = AVDSession._extract_guid(
            "/resource/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/details"
        )
        assert g == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_extract_guid_none(self):
        assert AVDSession._extract_guid("no guid") == ""


class TestPortalSession:
    def test_init_sets_backend(self):
        from azure_wrapper.config import AzureConfig
        config = AzureConfig(email="u@c.com", password="pw", backend="portal")
        s = PortalSession(config)
        assert s.config.backend == "portal"

    def test_init_overrides_avd(self):
        from azure_wrapper.config import AzureConfig
        config = AzureConfig(email="u@c.com", password="pw", backend="avd")
        s = PortalSession(config)
        assert s.config.backend == "portal"

    def test_parse_arm_response_empty(self):
        vms = PortalSession._parse_arm_response({"value": []})
        assert vms == []

    def test_parse_arm_response_with_vms(self):
        data = {
            "value": [
                {
                    "id": "/subscriptions/sub-1/.../vm-1",
                    "name": "prod-web-01",
                    "location": "eastus",
                    "properties": {
                        "instanceView": {
                            "statuses": [
                                {"code": "PowerState/running"},
                                {"code": "ProvisioningState/succeeded"},
                            ],
                        },
                    },
                },
                {
                    "id": "/subscriptions/sub-1/.../vm-2",
                    "name": "prod-db-01",
                    "location": "westeurope",
                    "properties": {},
                },
            ],
        }
        vms = PortalSession._parse_arm_response(data)
        assert len(vms) == 2
        assert vms[0].name == "prod-web-01"
        assert vms[0].power_state == "running"
        assert vms[0].location == "eastus"
        assert vms[0].backend == "portal"
        assert vms[0].kind == "virtualMachine"
        assert vms[1].name == "prod-db-01"
        assert vms[1].power_state == ""
        assert vms[1].location == "westeurope"

    def test_extract_arm_id(self):
        arm_id = PortalSession._extract_arm_id(
            'href="/subscriptions/abc-123/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-name"'
        )
        # The regex extracts the full ARM resource path
        assert arm_id.startswith("/subscriptions/abc-123")
        assert "virtualMachines/vm-name" in arm_id

    def test_extract_arm_id_none(self):
        assert PortalSession._extract_arm_id("no match") == ""


class TestPackageExports:
    def test_all_exports(self):
        assert hasattr(azure_wrapper, "AzureConfig")
        assert hasattr(azure_wrapper, "AVDSession")
        assert hasattr(azure_wrapper, "PortalSession")
        assert hasattr(azure_wrapper, "VmInfo")
        assert hasattr(azure_wrapper, "VmConnection")
        assert hasattr(azure_wrapper, "SessionState")
        assert hasattr(azure_wrapper, "Credentials")

    def test_azure_session_is_abstract(self):
        from azure_wrapper.session import AzureSession
        import inspect
        assert inspect.isabstract(AzureSession)


class TestConstants:
    def test_chromium_args(self):
        assert any("SharedArrayBuffer" in a for a in CHROMIUM_ARGS)
        assert any("CrossOriginOpenerPolicy" in a for a in CHROMIUM_ARGS)

    def test_edge_ua(self):
        assert "Windows NT 10.0" in EDGE_UA
        assert "Edg/" in EDGE_UA


class TestResolution:
    """Tests for azure_wrapper.resolution — dynamic viewport resolution."""

    def test_clamp_min(self):
        from azure_wrapper.resolution import clamp
        assert clamp(100, 100) == {"width": 800, "height": 600}

    def test_clamp_max(self):
        from azure_wrapper.resolution import clamp
        assert clamp(10000, 5000) == {"width": 7680, "height": 4320}

    def test_clamp_normal(self):
        from azure_wrapper.resolution import clamp
        assert clamp(1920, 1080) == {"width": 1920, "height": 1080}

    def test_parse_none(self):
        from azure_wrapper.resolution import parse
        assert parse(None) is None

    def test_parse_empty(self):
        from azure_wrapper.resolution import parse
        assert parse("") is None

    def test_parse_auto(self):
        from azure_wrapper.resolution import parse
        assert parse("auto") is None

    def test_parse_preset_desktop(self):
        from azure_wrapper.resolution import parse
        assert parse("desktop") == {"width": 1920, "height": 1080}

    def test_parse_preset_laptop(self):
        from azure_wrapper.resolution import parse
        assert parse("laptop") == {"width": 1366, "height": 768}

    def test_parse_preset_hd(self):
        from azure_wrapper.resolution import parse
        assert parse("hd") == {"width": 1280, "height": 720}

    def test_parse_wxh(self):
        from azure_wrapper.resolution import parse
        assert parse("1920x1080") == {"width": 1920, "height": 1080}

    def test_parse_wxh_laptop(self):
        from azure_wrapper.resolution import parse
        assert parse("1366x768") == {"width": 1366, "height": 768}

    def test_parse_dict(self):
        from azure_wrapper.resolution import parse
        assert parse({"width": 1024, "height": 768}) == {"width": 1024, "height": 768}

    def test_parse_bad_string(self):
        from azure_wrapper.resolution import parse
        assert parse("garbage") is None

    def test_resolve_configured(self):
        from azure_wrapper.resolution import resolve
        assert resolve(configured="desktop") == {"width": 1920, "height": 1080}

    def test_resolve_none_fallback(self):
        from azure_wrapper.resolution import resolve, FALLBACK
        assert resolve(configured=None) == FALLBACK

    def test_resolve_client_viewport(self):
        from azure_wrapper.resolution import resolve
        result = resolve(
            configured=None,
            client_viewport={"width": 1366, "height": 768},
        )
        assert result == {"width": 1366, "height": 768}

    def test_resolve_auto_with_client(self):
        from azure_wrapper.resolution import resolve
        result = resolve(
            configured="auto",
            client_viewport={"width": 1024, "height": 768},
        )
        assert result == {"width": 1024, "height": 768}

    def test_resolve_custom_fallback(self):
        from azure_wrapper.resolution import resolve
        result = resolve(
            configured=None,
            fallback={"width": 1280, "height": 720},
        )
        assert result == {"width": 1280, "height": 720}

    def test_config_has_resolution_field(self):
        c = AzureConfig(email="u@c.com", password="pw")
        assert c.resolution is None

    def test_config_resolution_custom(self):
        c = AzureConfig(
            email="u@c.com", password="pw", resolution="laptop"
        )
        assert c.resolution == "laptop"

    def test_config_viewport_still_works(self):
        c = AzureConfig(email="u@c.com", password="pw")
        assert c.viewport == {"width": 1920, "height": 1080}

    def test_client_viewport_wins_over_fallback(self):
        """When resolution is None, client viewport beats the fallback viewport dict."""
        from azure_wrapper.resolution import resolve
        result = resolve(
            configured=None,
            client_viewport={"width": 1366, "height": 768},
            fallback={"width": 1920, "height": 1080},
        )
        assert result == {"width": 1366, "height": 768}

    def test_configured_still_beats_client(self):
        """Explicit resolution beats client viewport even with fallback."""
        from azure_wrapper.resolution import resolve
        result = resolve(
            configured="laptop",
            client_viewport={"width": 2560, "height": 1440},
            fallback={"width": 1920, "height": 1080},
        )
        assert result == {"width": 1366, "height": 768}


class TestGetResolution:
    """Tests for the get_resolution() convenience API."""

    def test_defaults_to_fallback(self):
        from azure_wrapper.resolution import get_resolution, FALLBACK
        # With no DISPLAY env and no override, should get fallback
        result = get_resolution(auto_detect=False)
        assert result == FALLBACK

    def test_configured_preset(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured="laptop", auto_detect=False)
        assert result == {"width": 1366, "height": 768}

    def test_configured_wxh(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured="1280x800", auto_detect=False)
        assert result == {"width": 1280, "height": 800}

    def test_configured_dict(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured={"width": 1024, "height": 768}, auto_detect=False)
        assert result == {"width": 1024, "height": 768}

    def test_client_viewport_wins_over_fallback(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(
            configured=None,
            client_viewport={"width": 1366, "height": 768},
            auto_detect=False,
        )
        assert result == {"width": 1366, "height": 768}

    def test_configured_beats_client(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(
            configured="hd",
            client_viewport={"width": 2560, "height": 1440},
            auto_detect=False,
        )
        assert result == {"width": 1280, "height": 720}

    def test_custom_fallback(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(fallback={"width": 800, "height": 600}, auto_detect=False)
        assert result == {"width": 800, "height": 600}

    def test_clamps_too_small(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured={"width": 100, "height": 100}, auto_detect=False)
        assert result == {"width": 800, "height": 600}

    def test_clamps_too_large(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured={"width": 10000, "height": 10000}, auto_detect=False)
        assert result == {"width": 7680, "height": 4320}

    def test_auto_string_resolves_to_fallback(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured="auto", auto_detect=False)
        assert result["width"] > 0
        assert result["height"] > 0

    def test_auto_detect_disabled_skips_system(self):
        from azure_wrapper.resolution import get_resolution
        result = get_resolution(configured=None, auto_detect=False)
        assert result == {"width": 1920, "height": 1080}  # FALLBACK

    def test_detect_system_resolution_no_display(self, monkeypatch):
        """When no display env vars are set, returns None."""
        from azure_wrapper.resolution import detect_system_resolution
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        result = detect_system_resolution()
        assert result is None

    def test_parse_xrandr_current_mode(self):
        from azure_wrapper.resolution import _parse_xrandr
        output = (
            "Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192\n"
            "DP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis)\n"
            "   1920x1080     60.00*+  59.94    50.00\n"
            "   1680x1050     59.88\n"
        )
        result = _parse_xrandr(output)
        assert result == {"width": 1920, "height": 1080}

    def test_parse_xrandr_no_star(self):
        from azure_wrapper.resolution import _parse_xrandr
        # No mode with '*' (current) marker
        output = "DP-1 connected\n   1920x1080     60.00\n"
        result = _parse_xrandr(output)
        assert result is None

    def test_parse_wlr_randr(self):
        from azure_wrapper.resolution import _parse_wlr_randr
        output = (
            "DP-1: 1920x1080@60Hz\n"
            "  Mode: 1920x1080@60Hz\n"
            "  Mode: 2560x1440@60Hz\n"
        )
        result = _parse_wlr_randr(output)
        # First match (the Mode line with 1920x1080)
        assert result == {"width": 1920, "height": 1080}

    def test_parse_xdpyinfo(self):
        from azure_wrapper.resolution import _parse_xdpyinfo
        output = "screen #0:\n  dimensions:    1920x1080 pixels (508x285 millimeters)\n  resolution:    96x96 dots per inch\n"
        result = _parse_xdpyinfo(output)
        assert result == {"width": 1920, "height": 1080}


class TestValidateResolution:
    """Tests for validate_resolution()."""

    def test_valid_preset(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution("desktop") is None
        assert validate_resolution("laptop") is None
        assert validate_resolution("hd") is None
        assert validate_resolution("fhd") is None
        assert validate_resolution("qhd") is None
        assert validate_resolution("4k") is None

    def test_valid_wxh(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution("1920x1080") is None
        assert validate_resolution("1366x768") is None
        assert validate_resolution("800x600") is None

    def test_valid_dict(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution({"width": 1920, "height": 1080}) is None

    def test_none_rejected(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution(None) == "Resolution spec must not be None"

    def test_auto_rejected(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution("auto")
        assert msg is not None
        assert "auto" in msg.lower()

    def test_empty_string_rejected(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution("") == "Resolution string must not be empty"

    def test_garbage_rejected(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution("garbage")
        assert msg is not None
        assert "unrecognised" in msg.lower()

    def test_dict_missing_keys(self):
        from azure_wrapper.resolution import validate_resolution
        assert validate_resolution({"width": 1920}) is not None
        assert validate_resolution({"height": 1080}) is not None
        assert validate_resolution({}) is not None

    def test_dict_too_small(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution({"width": 100, "height": 100})
        assert msg is not None
        assert "at least" in msg.lower()

    def test_dict_too_large(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution({"width": 10000, "height": 10000})
        assert msg is not None
        assert "exceed" in msg.lower()

    def test_dict_non_int(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution({"width": "foo", "height": 1080})
        assert msg is not None

    def test_invalid_type(self):
        from azure_wrapper.resolution import validate_resolution
        msg = validate_resolution(42)
        assert msg is not None
        assert "int" in msg.lower()

    def test_get_resolution_package_export(self):
        """get_resolution is importable from the package root."""
        from azure_wrapper import get_resolution
        result = get_resolution(configured="hd", auto_detect=False)
        assert result == {"width": 1280, "height": 720}
