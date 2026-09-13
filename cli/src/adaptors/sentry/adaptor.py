"""Sentry adaptor — resolves Sentry issue URLs to repos and provides auth."""

from __future__ import annotations

from pathlib import Path

from src.adaptors.sentry.auth import resolve_token
from src.adaptors.sentry.client import resolve_api_url
from src.adaptors.sentry.display import SentryDisplay
from src.adaptors.sentry.resolver import resolve_repo_url


class SentryAdaptor:
    """Adaptor for Sentry issue URLs."""

    @property
    def name(self) -> str:
        return "sentry"

    @property
    def default_command(self) -> str:
        return "sentry"

    @property
    def skills(self) -> dict[str, str]:
        return {"sentry": "sentry-fix"}

    def matches(self, url: str) -> bool:
        # Covers SaaS (sentry.io) and self-hosted instances whose domain
        # contains "sentry" (e.g. sentry.example.com) — same heuristic the
        # gitlab adaptor's trigger uses for self-hosted GitLab.
        return "sentry" in url

    def select_command(self, url: str) -> str:  # noqa: ARG002
        return self.default_command

    def resolve_auth(self) -> str | None:
        return resolve_token()

    def resolve_repo_url(
        self, issue_url: str, token: str, *, repo_override: str | None = None
    ) -> str | None:
        return resolve_repo_url(issue_url, token, repo_override=repo_override)

    def fetch_sha(self, url: str, token: str) -> str | None:  # noqa: ARG002
        return None

    def fetch_head_ref(self, url: str, token: str) -> str | None:  # noqa: ARG002
        return None

    def fetch_context(self, repo_dir: Path, url: str, token: str) -> None:  # noqa: ARG002
        # Sentry-fix's plugin script (fetch-sentry-data.py) handles its own
        # data fetching with skill-specific shaping. No-op here.
        return

    def build_env(self, token: str, issue_urls: list[str]) -> dict[str, str]:
        env: dict[str, str] = {}
        if token:
            env["SENTRY_AUTH_TOKEN"] = token
        first_url = issue_urls[0] if issue_urls else ""
        api_url = resolve_api_url(first_url, token or None)
        if api_url and api_url != "https://sentry.io":
            env["SENTRY_API_URL"] = api_url
        return env

    def build_prompt(self, issue_urls: list[str]) -> str:
        return " ".join(issue_urls)

    @property
    def display(self) -> SentryDisplay:
        return SentryDisplay()
