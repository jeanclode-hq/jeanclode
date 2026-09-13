"""GitLab adaptor — reviews and summarizes GitLab MRs (gitlab.com + self-hosted)."""

from __future__ import annotations

from pathlib import Path

from src.adaptors.gitlab.auth import resolve_token
from src.adaptors.gitlab.client import has_note_fragment, parse_issue_url, parse_mr_url
from src.adaptors.gitlab.context import fetch_mr_context, fetch_respond_context
from src.adaptors.gitlab.display import GitlabDisplay
from src.adaptors.gitlab.resolver import resolve_repo_url
from src.adaptors.gitlab.sha import fetch_mr_head_ref, fetch_mr_head_sha


class GitlabAdaptor:
    """Adaptor for GitLab MR / issue URLs."""

    @property
    def name(self) -> str:
        return "gitlab"

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
        return parse_mr_url(url) is not None or parse_issue_url(url) is not None

    def select_command(self, url: str) -> str:
        if has_note_fragment(url):
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
        return fetch_mr_head_sha(url, token)

    def fetch_head_ref(self, url: str, token: str) -> str | None:
        return fetch_mr_head_ref(url, token)

    def fetch_context(self, repo_dir: Path, url: str, token: str) -> None:
        if parse_mr_url(url) is not None:
            fetch_mr_context(repo_dir, url, token)
        fetch_respond_context(repo_dir, url, token)

    def build_env(self, token: str, issue_urls: list[str]) -> dict[str, str]:
        env: dict[str, str] = {"GITLAB_TOKEN": token}
        first = issue_urls[0] if issue_urls else ""
        parsed = parse_mr_url(first)
        if parsed:
            host, _, _ = parsed
            if host != "gitlab.com":
                env["GITLAB_HOST"] = f"https://{host}"
        return env

    def build_prompt(self, issue_urls: list[str]) -> str:
        return " ".join(issue_urls)

    @property
    def display(self) -> GitlabDisplay:
        return GitlabDisplay()
