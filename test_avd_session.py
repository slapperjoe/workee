"""Unit tests for avd_session module (no browser required)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from avd_session import SessionState, AVDSessionManager, _resolve_vm_selection


class TestSessionState:
    def test_default_state(self):
        state = SessionState()
        assert state.authenticated is False
        assert state.on_dashboard is False
        assert state.current_url == ""
        assert state.vm_count == 0
        assert state.reauth_count == 0
        assert state.last_reauth_time == 0.0
        assert state.uptime_seconds == 0.0

    def test_active_state(self):
        state = SessionState(
            authenticated=True,
            on_dashboard=True,
            current_url="https://windows.cloud.microsoft/#/devices",
            vm_count=5,
            reauth_count=2,
            last_reauth_time=123456.0,
            uptime_seconds=3600.0,
        )
        assert state.authenticated is True
        assert state.on_dashboard is True
        assert "devices" in state.current_url
        assert state.vm_count == 5
        assert state.reauth_count == 2


class TestFeedParsing:
    def test_parse_standard_response(self):
        data = {
            "value": [
                {
                    "workspace": {"name": "My Workspace", "id": "ws-1"},
                    "resources": [
                        {
                            "name": "Session Desktop",
                            "resourceType": "Desktop",
                            "resourceId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                            "icon": "data:image/png;base64,...",
                        },
                        {
                            "name": "Outlook",
                            "resourceType": "RemoteApp",
                            "resourceId": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                            "icon": "",
                        },
                    ],
                },
            ],
        }
        vms = AVDSessionManager._parse_feed_response(data)
        assert len(vms) == 2
        assert vms[0]["name"] == "Session Desktop"
        assert vms[0]["kind"] == "Desktop"
        assert vms[0]["workspace"] == "My Workspace"
        assert vms[0]["resource_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert vms[1]["name"] == "Outlook"
        assert vms[1]["kind"] == "RemoteApp"

    def test_parse_empty_response(self):
        vms = AVDSessionManager._parse_feed_response({"value": []})
        assert vms == []

    def test_parse_flat_list(self):
        data = [
            {"name": "VM-1", "resourceType": "Desktop", "resourceId": "guid-1"},
            {"name": "VM-2", "resourceType": "Desktop", "resourceId": "guid-2"},
        ]
        vms = AVDSessionManager._parse_feed_response(data)
        assert len(vms) == 2
        assert vms[0]["name"] == "VM-1"
        assert vms[1]["name"] == "VM-2"

    def test_parse_missing_keys(self):
        data = {
            "value": [
                {
                    "workspace": {},
                    "resources": [
                        {"name": "Bare VM"},
                    ],
                },
            ],
        }
        vms = AVDSessionManager._parse_feed_response(data)
        assert len(vms) == 1
        assert vms[0]["name"] == "Bare VM"
        assert vms[0]["kind"] == "unknown"
        assert vms[0]["workspace"] == "Unknown"


class TestGuidExtraction:
    def test_extract_guid_from_url(self):
        guid = AVDSessionManager._extract_guid(
            "https://example.com/resource/a1b2c3d4-e5f6-7890-abcd-ef1234567890/details"
        )
        assert guid == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_extract_guid_from_plain(self):
        guid = AVDSessionManager._extract_guid(
            "deadbeef-dead-beef-dead-beefdeadbeef"
        )
        assert guid == "deadbeef-dead-beef-dead-beefdeadbeef"

    def test_extract_guid_no_match(self):
        guid = AVDSessionManager._extract_guid("no guid here")
        assert guid == ""

    def test_extract_guid_first_match(self):
        guid = AVDSessionManager._extract_guid(
            "guid1: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee, guid2: ffffffff-gggg-hhhh-iiii-jjjjjjjjjjjj"
        )
        assert guid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class TestConstructor:
    def test_minimal_constructor(self):
        mgr = AVDSessionManager(email="u@c.com", password="secret")
        assert mgr.email == "u@c.com"
        assert mgr.password == "secret"
        assert mgr.mfa_method == "auto"
        assert mgr.totp_secret is None
        assert mgr.poll_interval == 15.0
        assert mgr.reauth_timeout == 300.0
        assert mgr.headed is False

    def test_full_constructor(self):
        mgr = AVDSessionManager(
            email="u@c.com",
            password="secret",
            mfa_method="totp",
            totp_secret="JBSWY3DPEHPK3PXP",
            mfa_timeout=60.0,
            mfa_post_submit_timeout=30.0,
            storage_state_path="/tmp/state.json",
            poll_interval=10.0,
            reauth_timeout=120.0,
            headed=True,
        )
        assert mgr.email == "u@c.com"
        assert mgr.mfa_method == "totp"
        assert mgr.totp_secret == "JBSWY3DPEHPK3PXP"
        assert mgr.mfa_timeout == 60.0
        assert mgr.mfa_post_submit_timeout == 30.0
        assert mgr.storage_state_path == Path("/tmp/state.json")
        assert mgr.poll_interval == 10.0
        assert mgr.reauth_timeout == 120.0
        assert mgr.headed is True


class TestConstants:
    def test_vm_selectors_not_empty(self):
        from avd_session import VM_SELECTORS
        assert len(VM_SELECTORS) >= 3

    def test_session_expired_indicators_not_empty(self):
        from avd_session import SESSION_EXPIRED_INDICATORS
        assert len(SESSION_EXPIRED_INDICATORS) >= 2

    def test_chromium_args_present(self):
        from avd_session import CHROMIUM_ARGS
        assert "--enable-features=SharedArrayBuffer" in CHROMIUM_ARGS
        assert "--enable-features=CrossOriginOpenerPolicy" in CHROMIUM_ARGS


class TestVMSelection:
    """Tests for _resolve_vm_selection — no browser needed."""

    SAMPLE_VMS = [
        {"name": "My Cloud PC", "kind": "CloudPC", "status": "Running"},
        {"name": "Dev Desktop", "kind": "desktop", "status": "Stopped"},
        {"name": "Outlook RemoteApp", "kind": "RemoteApp", "status": "Running"},
    ]

    def test_select_by_index(self):
        name = _resolve_vm_selection("1", self.SAMPLE_VMS)
        assert name == "My Cloud PC"

    def test_select_by_index_last(self):
        name = _resolve_vm_selection("3", self.SAMPLE_VMS)
        assert name == "Outlook RemoteApp"

    def test_select_by_index_out_of_range(self):
        name = _resolve_vm_selection("5", self.SAMPLE_VMS)
        assert name is None

    def test_select_by_name_exact(self):
        name = _resolve_vm_selection("My Cloud PC", self.SAMPLE_VMS)
        assert name == "My Cloud PC"

    def test_select_by_name_case_insensitive(self):
        name = _resolve_vm_selection("my cloud pc", self.SAMPLE_VMS)
        assert name == "My Cloud PC"

    def test_select_by_name_partial(self):
        name = _resolve_vm_selection("Dev", self.SAMPLE_VMS)
        assert name == "Dev Desktop"

    def test_select_by_name_ambiguous(self):
        vms = [
            {"name": "Dev Desktop", "kind": "desktop", "status": "Stopped"},
            {"name": "Prod Desktop", "kind": "desktop", "status": "Running"},
        ]
        name = _resolve_vm_selection("Desktop", vms)
        assert name is None  # ambiguous — two matches

    def test_select_by_name_no_match(self):
        name = _resolve_vm_selection("NonexistentVM", self.SAMPLE_VMS)
        assert name is None

    def test_select_empty_vms(self):
        name = _resolve_vm_selection("1", [])
        assert name is None

    def test_select_index_zero(self):
        name = _resolve_vm_selection("0", self.SAMPLE_VMS)
        assert name is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
