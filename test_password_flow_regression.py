import pytest
from unittest.mock import AsyncMock, MagicMock

from azure_wrapper.auth import AuthManager, AuthPhase, LOGIN_HOST
from azure_wrapper.config import AzureConfig


@pytest.mark.asyncio
async def test_mfa_prompt_switches_to_password_before_mfa_handling():
    auth = AuthManager(AzureConfig(email="user@example.com", password="secret"))
    page = AsyncMock()
    page.url = f"https://{LOGIN_HOST}/oauth2/authorize"
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    auth._is_authenticated = AsyncMock(return_value=False)
    auth._fill_email = AsyncMock()
    auth._detect_post_email_prompt = AsyncMock(return_value="mfa")
    auth._switch_to_password = AsyncMock(return_value=True)
    auth._fill_password = AsyncMock(return_value=True)
    auth._save_state = AsyncMock()

    result = await auth.authenticate(MagicMock(), MagicMock(), page, reuse_state=False)

    assert result is AuthPhase.PASSWORD_ENTERED
    auth._switch_to_password.assert_awaited_once_with(page)
    auth._fill_password.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_mfa_prompt_fails_without_password_switch():
    auth = AuthManager(AzureConfig(email="user@example.com", password="secret"))
    page = AsyncMock()
    page.url = f"https://{LOGIN_HOST}/oauth2/authorize"
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    auth._is_authenticated = AsyncMock(return_value=False)
    auth._fill_email = AsyncMock()
    auth._detect_post_email_prompt = AsyncMock(return_value="mfa")
    auth._switch_to_password = AsyncMock(return_value=False)
    auth._fill_password = AsyncMock()

    result = await auth.authenticate(MagicMock(), MagicMock(), page, reuse_state=False)

    assert result is AuthPhase.FAILED
    auth._fill_password.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_fill_retries_transient_visibility():
    auth = AuthManager(AzureConfig(email="user@example.com", password="secret"))
    page = AsyncMock()
    field = AsyncMock()
    field.is_visible.side_effect = [False] * 4 + [True]
    page.locator = MagicMock(return_value=MagicMock(first=field))

    assert await auth._fill_password(page)
    field.fill.assert_awaited_once_with("secret")
    page.click.assert_awaited_once()
    assert field.is_visible.await_count >= 2


@pytest.mark.asyncio
async def test_password_fill_returns_false_when_absent():
    auth = AuthManager(AzureConfig(email="user@example.com", password="secret"))
    page = AsyncMock()
    field = AsyncMock()
    field.is_visible.return_value = False
    page.locator = MagicMock(return_value=MagicMock(first=field))

    assert not await auth._fill_password(page)
    field.fill.assert_not_awaited()
    page.click.assert_not_awaited()
