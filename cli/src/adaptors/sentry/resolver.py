"""Resolve Sentry issue URLs to repository URLs.

Combines --repo CLI flag, Sentry code mappings, and URL parsing to
determine which repository to clone for each issue.
"""

from __future__ import annotations

import logging
import re

from src.adaptors.sentry.client import (
    fetch_code_mappings,
    fetch_issue_project_slug,
    resolve_api_url,
)

logger = logging.getLogger(__name__)


def _extract_issue_id(value: str) -> str:
    """Extract the Sentry issue ID from a URL or plain ID."""
    if re.fullmatch(r"\d+", value):
        return value
    match = re.search(r"/issues/(\d+)", value)
    return match.group(1) if match else ""


def _extract_org_slug(sentry_url: str) -> str | None:
    """Extract the org slug from a URL like https://org.sentry.io/..."""
    match = re.match(r"https://([^.]+)\.sentry\.io/", sentry_url)
    return match.group(1) if match else None


def resolve_repo_url(
    issue_url: str,
    token: str | None,
    *,
    repo_override: str | None = None,
) -> str | None:
    """Resolve a Sentry issue URL to a repository URL.

    Priority: --repo flag > code mapping match > None.
    """
    # CLI --repo flag takes precedence
    if repo_override:
        if not repo_override.startswith("http"):
            return f"https://github.com/{repo_override}"
        return repo_override

    # Try code mappings
    org_slug = _extract_org_slug(issue_url)
    issue_id = _extract_issue_id(issue_url)
    if not org_slug or not issue_id:
        return None

    api_url = resolve_api_url(issue_url, token)
    project_slug = fetch_issue_project_slug(api_url, issue_id, token)
    if not project_slug:
        return None

    mappings = fetch_code_mappings(api_url, org_slug, token)
    for mapping in mappings:
        if mapping.get("projectSlug") == project_slug:
            repo_name = mapping.get("repoName", "")
            if repo_name:
                if not repo_name.startswith("http"):
                    return f"https://github.com/{repo_name}"
                return repo_name

    return None
