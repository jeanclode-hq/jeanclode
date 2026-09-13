"""Tests for CLI configuration."""

from __future__ import annotations

import pytest

from src.config import Config, SentryConfig


def test_sentry_config_stores_token() -> None:
    cfg = SentryConfig(auth_token="tok")
    assert cfg.auth_token == "tok"


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "test-token")
    cfg = Config.from_env()
    assert cfg.sentry.auth_token == "test-token"
    assert cfg.debug is False


def test_config_from_env_missing_token_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """In local mode, missing SENTRY_AUTH_TOKEN is OK (sentry-cli will be used)."""
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    cfg = Config.from_env()
    assert cfg.sentry.auth_token == ""


def test_config_from_env_missing_token_container_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """In container mode the agent runs sandboxed and the security-proxy
    sidecar injects auth on the wire — so missing tokens must NOT raise."""
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    cfg = Config.from_env()
    assert cfg.container_mode is True
    assert cfg.sentry.auth_token == ""
    assert cfg.claude_code is None
    assert cfg.git_forge is None


def test_config_debug_defaults_false() -> None:
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    assert cfg.debug is False


def test_claude_code_oauth_token_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-xyz")
    cfg = Config.from_env()
    assert cfg.claude_code is not None
    assert cfg.claude_code.oauth_token == "oauth-xyz"
    assert cfg.claude_code.api_key is None


def test_anthropic_api_key_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    cfg = Config.from_env()
    assert cfg.claude_code is not None
    assert cfg.claude_code.api_key == "sk-ant-123"
    assert cfg.claude_code.oauth_token is None


def test_container_mode_tolerates_missing_claude_code_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandboxed agents don't carry LLM credentials — proxy injects them."""
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    cfg = Config.from_env()
    assert cfg.claude_code is None
    assert cfg.container_mode is True


def test_container_mode_tolerates_missing_git_forge_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandboxed agents don't carry git tokens — proxy injects them."""
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config.from_env()
    assert cfg.git_forge is None
    assert cfg.container_mode is True


def test_gitlab_token_without_host_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    with pytest.raises(ValueError, match="GITLAB_HOST"):
        Config.from_env()


def test_both_tokens_missing_local_mode_fields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    cfg = Config.from_env()
    assert cfg.claude_code is None
    assert cfg.git_forge is None
    assert cfg.container_mode is False


def test_gh_token_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "ghp_abc")
    cfg = Config.from_env()
    assert cfg.git_forge is not None
    assert cfg.git_forge.gh_token == "ghp_abc"


def test_gitlab_token_with_host_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    monkeypatch.setenv("GITLAB_HOST", "gitlab.example.com")
    cfg = Config.from_env()
    assert cfg.git_forge is not None
    assert cfg.git_forge.gitlab_token == "glpat-xyz"
    assert cfg.git_forge.gitlab_host == "gitlab.example.com"


def test_container_mode_with_all_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-xyz")
    monkeypatch.setenv("GH_TOKEN", "ghp_123")
    cfg = Config.from_env()
    assert cfg.container_mode is True
    assert cfg.claude_code is not None
    assert cfg.git_forge is not None


# -- get_model priority --


def test_get_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_MODEL", raising=False)
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    assert cfg.get_model() == "sonnet"


def test_get_model_file_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_MODEL", raising=False)
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    cfg._file_config = {"models": {"model": "opus"}}
    assert cfg.get_model() == "opus"


def test_get_model_env_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEANCLODE_MODEL", "opus")
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    cfg._file_config = {"models": {"model": "haiku"}}
    assert cfg.get_model() == "opus"


def test_get_model_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEANCLODE_MODEL", "opus")
    cfg = Config(
        sentry=SentryConfig(auth_token="tok"),
        model_override="haiku",
    )
    assert cfg.get_model() == "haiku"


def test_from_env_with_model_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.delenv("JEANCLODE_MODEL", raising=False)
    file_config = {"models": {"model": "haiku"}}
    cfg = Config.from_env(model="opus", file_config=file_config)
    assert cfg.model_override == "opus"
    assert cfg._file_config == file_config


# -- get_small_model priority --


def test_get_small_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_SMALL_MODEL", raising=False)
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    assert cfg.get_small_model() == "haiku"


def test_get_small_model_file_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_SMALL_MODEL", raising=False)
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    cfg._file_config = {"models": {"small_model": "sonnet"}}
    assert cfg.get_small_model() == "sonnet"


def test_get_small_model_env_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEANCLODE_SMALL_MODEL", "opus")
    cfg = Config(sentry=SentryConfig(auth_token="tok"))
    cfg._file_config = {"models": {"small_model": "haiku"}}
    assert cfg.get_small_model() == "opus"
