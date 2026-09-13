"""Shared fixtures for CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config, SentryConfig


@pytest.fixture
def sentry_config() -> SentryConfig:
    return SentryConfig(auth_token="test-token-123")


@pytest.fixture
def config(sentry_config: SentryConfig) -> Config:
    return Config(sentry=sentry_config, debug=False)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip collecting ``tests/eval`` unless it was explicitly requested.

    ``-m not eval`` (the default addopts) only deselects eval items *after*
    collection — collection itself still imports ``tests/eval/conftest.py``,
    which imports ``deepeval``, which calls ``load_dotenv()`` on import. That
    walks up from this package's cwd and loads the repo-root ``.env`` (meant
    for the docker-compose stack) into the real process environment,
    silently polluting every other test in the same run with real secrets
    (e.g. ``CLAUDE_CODE_OAUTH_TOKEN``) that were never supposed to be set.
    Skipping collection entirely — not just deselecting afterward — is the
    only way to avoid the import. ``make eval`` still works: it passes
    ``tests/eval/`` explicitly, which this leaves uncollected-ignored.
    """
    if "eval" in collection_path.parts:
        return not any("eval" in arg for arg in config.invocation_params.args)
    return None
