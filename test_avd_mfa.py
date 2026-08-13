"""Unit tests for avd_mfa module (no Playwright/browser required)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from avd_mfa import MfaMethod, MfaResult, MfaStatus


class TestMfaResult:
    def test_success_result(self):
        r = MfaResult(
            success=True,
            status=MfaStatus.SUCCESS,
            reason="TOTP code submitted",
            method_used="totp",
            prompt_type="code_input",
            elapsed_seconds=2.5,
        )
        assert r.success is True
        assert r.status == MfaStatus.SUCCESS
        assert r.reason == "TOTP code submitted"
        assert r.method_used == "totp"
        assert r.prompt_type == "code_input"
        assert r.elapsed_seconds == 2.5

    def test_timeout_result(self):
        r = MfaResult(
            success=False,
            status=MfaStatus.TIMEOUT,
            reason="MFA prompt did not appear",
            elapsed_seconds=120.0,
        )
        assert r.success is False
        assert r.status == MfaStatus.TIMEOUT
        assert r.method_used is None

    def test_skipped_result(self):
        r = MfaResult(
            success=True,
            status=MfaStatus.SKIPPED,
            reason="No MFA required",
        )
        assert r.success is True
        assert r.status == MfaStatus.SKIPPED

    def test_unsupported_prompt(self):
        r = MfaResult(
            success=False,
            status=MfaStatus.UNSUPPORTED_PROMPT,
            reason="Unknown prompt",
            prompt_type="webauthn",
        )
        assert r.success is False
        assert r.status == MfaStatus.UNSUPPORTED_PROMPT
        assert r.prompt_type == "webauthn"


class TestMfaMethod:
    def test_from_string(self):
        assert MfaMethod("totp") == MfaMethod.TOTP
        assert MfaMethod("manual") == MfaMethod.MANUAL
        assert MfaMethod("auto") == MfaMethod.AUTO

    def test_invalid_raises(self):
        try:
            MfaMethod("invalid")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestMfaStatus:
    def test_all_statuses(self):
        assert MfaStatus.SUCCESS.value == "success"
        assert MfaStatus.TIMEOUT.value == "timeout"
        assert MfaStatus.NO_MFA_PROMPT.value == "no_mfa_prompt"
        assert MfaStatus.UNSUPPORTED_PROMPT.value == "unsupported_prompt"
        assert MfaStatus.ERROR.value == "error"
        assert MfaStatus.SKIPPED.value == "skipped"


class TestSelectorConstants:
    def test_code_input_selectors_not_empty(self):
        from avd_mfa import MFA_CODE_INPUT_SELECTORS
        assert len(MFA_CODE_INPUT_SELECTORS) >= 2

    def test_push_selectors_not_empty(self):
        from avd_mfa import MFA_PUSH_SELECTORS
        assert len(MFA_PUSH_SELECTORS) >= 1

    def test_device_selection_selectors_not_empty(self):
        from avd_mfa import MFA_DEVICE_SELECTION_SELECTORS
        assert len(MFA_DEVICE_SELECTION_SELECTORS) >= 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
