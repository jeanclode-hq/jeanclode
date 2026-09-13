"""Resolve GitLab MR URLs to repository URLs for cloning."""

from __future__ import annotations

from src.adaptors.gitlab.client import parse_issue_url, parse_mr_url


def resolve_repo_url(
    issue_url: str,
    token: str,  # noqa: ARG001 — kept for protocol parity
    *,
    repo_override: str | None = None,
) -> str | None:
    """Resolve a GitLab MR or issue URL to its repository clone URL."""
    if repo_override:
        if not repo_override.startswith("http"):
            return f"https://gitlab.com/{repo_override}"
        return repo_override

    parsed = parse_mr_url(issue_url) or parse_issue_url(issue_url)
    if not parsed:
        return None
    host, project, _ = parsed
    return f"https://{host}/{project}"
