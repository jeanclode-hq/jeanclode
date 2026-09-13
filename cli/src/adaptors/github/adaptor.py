"""GitHub adaptor — reviews and summarizes GitHub PRs."""

from __future__ import annotations

from pathlib import Path

from src.adaptors.github.auth import resolve_token
from src.adaptors.github.client import (
    has_comment_fragment,
    parse_issue_url,
    parse_pr_url,
)
from src.adaptors.github.context import fetch_pr_context, fetch_respond_context
from src.adaptors.github.display import GithubDisplay
from src.adaptors.github.resolver import resolve_repo_url
from src.adaptors.github.sha import fetch_pr_head_ref, fetch_pr_head_sha


class GithubAdaptor:
    """Adaptor for GitHub PR / issue URLs."""

    @property
    def name(self) -> str:
        return "github"

    @property
    def default_command(self) -> str:
        return "review"

    @property
    def skills(self) -> dict[str, str]:
        return {
            "review": "code-review",
            "summary": "pr-summary",
            "respond": "jeanclode-respond",
            "issue-resolve": "issue-resolve",
        }

    def matches(self, url: str) -> bool:
        return parse_pr_url(url) is not None or parse_issue_url(url) is not None

    def select_command(self, url: str) -> str:
        # A comment fragment (e.g. ``#discussion_r123``) flips us to the
        # respond workflow regardless of whether the parent is a PR or
        # an issue. Bare issue URL → issue-resolve. Otherwise → default.
        if has_comment_fragment(url):
            return "respond"
        if parse_issue_url(url) is not None:
            return "issue-resolve"
        return self.default_command

    def resolve_auth(self) -> str | None:
        return resolve_token()

    def resolve_repo_url(
        self, issue_url: str, token: str, *, repo_override: str | None = None
    ) -> str | None:
        return resolve_repo_url(issue_url, token, repo_override=repo_override)

    def fetch_sha(self, url: str, token: str) -> str | None:
        return fetch_pr_head_sha(url, token)

    def fetch_head_ref(self, url: str, token: str) -> str | None:
        return fetch_pr_head_ref(url, token)

    def fetch_context(self, repo_dir: Path, url: str, token: str) -> None:
        # PR context fetch only works for PR URLs; issue URLs skip cleanly.
        if parse_pr_url(url) is not None:
            fetch_pr_context(repo_dir, url, token)
        # In respond mode (env vars set by the backend), populate the
        # mention-specific cache too. Safe to call for both PR and issue
        # surfaces; idempotent when env vars are absent.
        fetch_respond_context(repo_dir, url, token)

    def build_env(self, token: str, issue_urls: list[str]) -> dict[str, str]:  # noqa: ARG002
        return {"GITHUB_TOKEN": token, "GH_TOKEN": token}

    def build_prompt(self, issue_urls: list[str]) -> str:
        return " ".join(issue_urls)

    @property
    def display(self) -> GithubDisplay:
        return GithubDisplay()
