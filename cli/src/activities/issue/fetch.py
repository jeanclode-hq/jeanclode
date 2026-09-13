"""Fetch issue body, title, and comments from GitHub or GitLab."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from src.activities.decorator import activity
from src.activities.issue.schemas import IssueContext
from src.activities.issue.utils import _run_env
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def _paginate(cmd_for_page: Callable[[int], list[str]], *, cwd: Path, env: dict) -> list[dict]:
    """Walk a paginated `gh api` / `glab api` REST endpoint page-by-page.

    A single `gh issue view --json comments` / `glab issue view` call caps
    out at one page, so a long-lived issue can silently lose its oldest
    comments. Paging manually (rather than `--paginate`, which concatenates
    one JSON array per page into stdout) keeps each response a single JSON
    document.
    """
    items: list[dict] = []
    page = 1
    while True:
        cmd = cmd_for_page(page)
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
        cmd_str = " ".join(cmd)
        if proc.returncode != 0:
            logger.debug("%s failed: %s", cmd_str, proc.stderr)
            return items
        try:
            chunk = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.debug("Failed to parse output of %s", cmd_str, exc_info=True)
            return items
        if not isinstance(chunk, list) or not chunk:
            return items
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1


def _gh_paginate(path: str, *, cwd: Path, env: dict) -> list[dict]:
    sep = "&" if "?" in path else "?"
    return _paginate(
        lambda page: ["gh", "api", f"{path}{sep}per_page=100&page={page}"], cwd=cwd, env=env
    )


def _glab_paginate(endpoint: str, *, project: str, cwd: Path, env: dict) -> list[dict]:
    return _paginate(
        lambda page: ["glab", "api", "-R", project, f"{endpoint}?per_page=100&page={page}"],
        cwd=cwd,
        env=env,
    )


@activity(name="Fetching issue context")
def fetch_issue_context(
    issue_url: str,
    provider: str,
    repo: str,
    issue_number: str,
    *,
    ctx: RunContext,
) -> IssueContext:
    """Fetch issue body, title, and comments via gh (GitHub) or glab (GitLab)."""
    if provider == "github":
        return _fetch_github(issue_url, repo, issue_number, ctx=ctx)
    return _fetch_gitlab(issue_url, repo, issue_number, ctx=ctx)


def _fetch_github(
    issue_url: str,
    repo: str,
    issue_number: str,
    *,
    ctx: RunContext,
) -> IssueContext:
    env = _run_env(ctx)
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                issue_number,
                "-R",
                repo,
                "--json",
                "number,title,body,url",
            ],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        data = json.loads(proc.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        logger.warning("gh issue view failed for %s: %s", issue_url, exc)
        return IssueContext(
            issue_url=issue_url,
            provider="github",
            repo=repo,
            issue_number=issue_number,
            issue_title="",
            issue_body="(could not fetch issue body)",
            comments="",
        )

    comments_list = _gh_paginate(
        f"repos/{repo}/issues/{issue_number}/comments", cwd=ctx.cwd, env=env
    )
    formatted = "\n\n".join(
        f"**{c.get('user', {}).get('login', 'unknown')}:** {c.get('body', '')}"
        for c in comments_list
    )
    return IssueContext(
        issue_url=issue_url,
        provider="github",
        repo=repo,
        issue_number=issue_number,
        issue_title=data.get("title", ""),
        issue_body=data.get("body", "") or "",
        comments=formatted,
    )


def _fetch_gitlab(
    issue_url: str,
    repo: str,
    issue_number: str,
    *,
    ctx: RunContext,
) -> IssueContext:
    # repo is "host/project/path"; glab expects "project/path" + GITLAB_HOST for self-hosted
    parts = repo.split("/", 1)
    host = parts[0]
    project = parts[1] if len(parts) > 1 else repo
    env = {**_run_env(ctx), "GITLAB_HOST": f"https://{host}"}

    try:
        proc = subprocess.run(
            ["glab", "issue", "view", issue_number, "-R", project, "--output", "json"],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        data = json.loads(proc.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        logger.warning("glab issue view failed for %s: %s", issue_url, exc)
        return IssueContext(
            issue_url=issue_url,
            provider="gitlab",
            repo=repo,
            issue_number=issue_number,
            issue_title="",
            issue_body="(could not fetch issue body)",
            comments="",
        )

    slug = quote(project, safe="")
    notes = _glab_paginate(
        f"projects/{slug}/issues/{issue_number}/notes", project=project, cwd=ctx.cwd, env=env
    )
    formatted = "\n\n".join(
        f"**{n.get('author', {}).get('username', 'unknown')}:** {n.get('body', '')}" for n in notes
    )
    return IssueContext(
        issue_url=issue_url,
        provider="gitlab",
        repo=repo,
        issue_number=issue_number,
        issue_title=data.get("title", ""),
        issue_body=data.get("description", "") or "",
        comments=formatted,
    )
