"""Post a comment on a GitHub issue or GitLab issue."""

from __future__ import annotations

import logging
import subprocess

from src.activities.decorator import activity
from src.activities.issue.utils import _run_env
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


@activity(name="Posting issue comment")
def post_issue_comment(
    body: str,
    provider: str,
    repo: str,
    issue_number: str,
    *,
    ctx: RunContext,
) -> None:
    """Post ``body`` as a comment on the given issue via gh or glab."""
    if provider == "github":
        _post_github(repo, issue_number, body, ctx=ctx)
    else:
        _post_gitlab(repo, issue_number, body, ctx=ctx)


def _post_github(
    repo: str,
    issue_number: str,
    body: str,
    *,
    ctx: RunContext,
) -> None:
    try:
        subprocess.run(
            ["gh", "issue", "comment", issue_number, "-R", repo, "--body", body],
            cwd=ctx.cwd,
            check=True,
            capture_output=True,
            env=_run_env(ctx),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("gh issue comment failed for %s#%s: %s", repo, issue_number, exc)
        raise


def _post_gitlab(
    repo: str,
    issue_number: str,
    body: str,
    *,
    ctx: RunContext,
) -> None:
    # repo is "host/project/path"; glab expects "project/path" + GITLAB_HOST for self-hosted
    parts = repo.split("/", 1)
    host = parts[0]
    project = parts[1] if len(parts) > 1 else repo
    env = {**_run_env(ctx), "GITLAB_HOST": f"https://{host}"}
    try:
        subprocess.run(
            ["glab", "issue", "note", issue_number, "-R", project, "-m", body],
            cwd=ctx.cwd,
            check=True,
            capture_output=True,
            env=env,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("glab issue note failed for %s#%s: %s", project, issue_number, exc)
        raise
