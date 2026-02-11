import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from miniclaw.cli import commands as cli_commands
from miniclaw.config.loader import load_config
from miniclaw.config.schema import Config
from miniclaw.providers.oauth import (
    OAuthDeviceFlow,
    OAuthTokenSet,
    resolve_oauth_token_ref,
    save_token_to_store,
)
from miniclaw.secrets import SecretStore


class _FakeOAuthAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def start_device_flow(self) -> OAuthDeviceFlow:
        return OAuthDeviceFlow(
            device_code=f"{self.provider}-device",
            user_code=f"{self.provider}-code",
            verification_uri=f"https://example.test/{self.provider}/verify",
            verification_uri_complete=f"https://example.test/{self.provider}/verify?code=1",
            interval=1,
            expires_in=60,
        )

    def poll_for_token(self, _flow: OAuthDeviceFlow) -> OAuthTokenSet:
        return OAuthTokenSet(
            access_token=f"{self.provider}-access-token",
            refresh_token=f"{self.provider}-refresh-token",
            expires_at=int(time.time()) + 3600,
        )

    def refresh_token(self, _refresh_token: str) -> OAuthTokenSet:
        return OAuthTokenSet(
            access_token=f"{self.provider}-refreshed-token",
            refresh_token=f"{self.provider}-refresh-token",
            expires_at=int(time.time()) + 3600,
        )

    def revoke_token(self, _token: str) -> bool:
        return True


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
def test_auth_login_status_logout_for_supported_providers(tmp_path, monkeypatch, provider_name: str) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "miniclaw.providers.oauth.get_oauth_adapter",
        lambda provider: _FakeOAuthAdapter(provider),
    )

    runner = CliRunner()

    login = runner.invoke(
        cli_commands.app,
        ["auth", "login", "--provider", provider_name, "--no-browser"],
    )
    assert login.exit_code == 0, login.output

    cfg = load_config()
    provider_cfg = getattr(cfg.providers, provider_name)
    assert provider_cfg.auth_mode == "oauth"
    token_ref = resolve_oauth_token_ref(provider_name, provider_cfg.oauth_token_ref)
    store = SecretStore(namespace="miniclaw", backend="file", home=tmp_path)
    assert store.get(token_ref) is not None

    status = runner.invoke(cli_commands.app, ["auth", "status"])
    assert status.exit_code == 0, status.output
    assert provider_name in status.output

    logout = runner.invoke(
        cli_commands.app,
        ["auth", "logout", "--provider", provider_name, "--no-revoke"],
    )
    assert logout.exit_code == 0, logout.output

    cfg_after = load_config()
    provider_cfg_after = getattr(cfg_after.providers, provider_name)
    assert provider_cfg_after.auth_mode == "api_key"
    assert store.get(token_ref) is None


def test_make_provider_prefers_oauth_token_when_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    config = Config()
    config.agents.defaults.model = "openai/gpt-5-mini"
    config.providers.openai.auth_mode = "oauth"
    config.providers.openai.oauth_token_ref = "oauth:openai:token"
    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    save_token_to_store(
        store,
        "oauth:openai:token",
        OAuthTokenSet(access_token="oauth-access", refresh_token="oauth-refresh", expires_at=int(time.time()) + 3600),
    )

    provider = cli_commands._make_provider(  # noqa: SLF001
        config,
        model=config.agents.defaults.model,
        secret_store=store,
    )
    assert provider.api_key == "oauth-access"


def test_make_provider_falls_back_to_api_key_when_oauth_refresh_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    config = Config()
    config.agents.defaults.model = "openai/gpt-5-mini"
    config.providers.openai.auth_mode = "oauth"
    config.providers.openai.oauth_token_ref = "oauth:openai:token"
    config.providers.openai.api_key = "api-key-fallback"
    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    save_token_to_store(
        store,
        "oauth:openai:token",
        OAuthTokenSet(
            access_token="stale",
            refresh_token="refresh",
            expires_at=int(time.time()) - 3600,
        ),
    )

    class _FailRefresh:
        def refresh_token(self, _refresh_token: str):
            raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        "miniclaw.providers.oauth.get_oauth_adapter",
        lambda _provider: _FailRefresh(),
    )

    provider = cli_commands._make_provider(  # noqa: SLF001
        config,
        model=config.agents.defaults.model,
        secret_store=store,
    )
    assert provider.api_key == "api-key-fallback"


def test_make_provider_falls_back_to_other_provider_when_primary_oauth_unusable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "file")
    config = Config()
    config.agents.defaults.model = "openai/gpt-5-mini"
    config.providers.openai.auth_mode = "oauth"
    config.providers.openai.api_key = ""
    config.providers.openai.oauth_token_ref = "oauth:openai:token"
    config.providers.openrouter.api_key = "openrouter-fallback-key"

    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    provider = cli_commands._make_provider(  # noqa: SLF001
        config,
        model=config.agents.defaults.model,
        secret_store=store,
    )
    assert provider.api_key == "openrouter-fallback-key"
    assert provider.api_base == "https://openrouter.ai/api/v1"
