"""Configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel, Field, PrivateAttr

from src.config_file import get_config_model, get_config_small_model

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sonnet"
DEFAULT_SMALL_MODEL = "haiku"


class SentryConfig(BaseModel):
    """Sentry API configuration."""

    auth_token: str = Field(
        default="", description="Sentry API auth token (optional if using sentry-cli)"
    )
    api_url: str = Field(
        default="https://sentry.io",
        description="Sentry API base URL (e.g. https://de.sentry.io for EU region)",
    )


class ClaudeCodeConfig(BaseModel):
    """Claude Code authentication configuration."""

    oauth_token: str | None = None
    api_key: str | None = None


class GitForgeConfig(BaseModel):
    """Git forge authentication configuration."""

    gh_token: str | None = None
    gitlab_token: str | None = None
    gitlab_host: str | None = None


class Config(BaseModel):
    """Top-level CLI configuration."""

    sentry: SentryConfig
    debug: bool = False
    container_mode: bool = False
    claude_code: ClaudeCodeConfig | None = None
    git_forge: GitForgeConfig | None = None
    model_override: str | None = None
    _file_config: dict | None = PrivateAttr(default=None)

    def get_model(self) -> str:
        """Resolve model for agents. Priority: CLI > env > file > default."""
        # CLI override
        if self.model_override:
            return self.model_override

        # Env var override
        env_val = os.environ.get("JEANCLODE_MODEL")
        if env_val:
            return env_val

        # Config file
        if self._file_config:
            file_val = get_config_model(self._file_config)
            if file_val:
                return file_val

        return DEFAULT_MODEL

    def get_small_model(self) -> str:
        """Resolve small model for low-tier agents. Priority: env → file → default."""
        env_val = os.environ.get("JEANCLODE_SMALL_MODEL")
        if env_val:
            return env_val

        if self._file_config:
            file_val = get_config_small_model(self._file_config)
            if file_val:
                return file_val

        return DEFAULT_SMALL_MODEL

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        file_config: dict | None = None,
    ) -> Config:
        """Build config from environment variables."""
        auth_token = os.environ.get("SENTRY_AUTH_TOKEN", "")

        container_mode = os.environ.get("JEANCLODE_CONTAINER_MODE") == "1"

        # Container mode runs sandboxed: the security-proxy sidecar injects
        # auth on the wire, so the agent itself never sees real tokens. We
        # therefore *don't* require any of them to be present here.

        # Claude Code auth
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None
        api_key = os.environ.get("ANTHROPIC_API_KEY") or None
        claude_code: ClaudeCodeConfig | None = None
        if oauth_token or api_key:
            claude_code = ClaudeCodeConfig(oauth_token=oauth_token, api_key=api_key)

        # Git forge auth
        gh_token = os.environ.get("GH_TOKEN") or None
        gitlab_token = os.environ.get("GITLAB_TOKEN") or None
        gitlab_host = os.environ.get("GITLAB_HOST") or None

        if gitlab_token and not gitlab_host and not container_mode:
            msg = "GITLAB_HOST is required when GITLAB_TOKEN is set"
            raise ValueError(msg)

        git_forge: GitForgeConfig | None = None
        if gh_token or gitlab_token:
            git_forge = GitForgeConfig(
                gh_token=gh_token, gitlab_token=gitlab_token, gitlab_host=gitlab_host
            )

        sentry_api_url = os.environ.get("SENTRY_API_URL", "").rstrip("/")
        # Strip /api/0 suffix if user included it — we store just the base URL
        if sentry_api_url.endswith("/api/0"):
            sentry_api_url = sentry_api_url[: -len("/api/0")]
        sentry_api_url = sentry_api_url or ""

        config = cls(
            sentry=SentryConfig(
                auth_token=auth_token, api_url=sentry_api_url or "https://sentry.io"
            ),
            container_mode=container_mode,
            claude_code=claude_code,
            git_forge=git_forge,
            model_override=model,
        )
        config._file_config = file_config
        return config
