"""``post_ready_notice`` — @-mention the tenant's notify list on a finished MR.

Both ends of the autonomous review loop call this: code review when it
converges on LGTM, and respond when a resolve-only turn clears the last
thread without pushing (so no re-review is triggered and no LGTM is ever
posted). Two emitters, one meaning — hence the marker check, which makes
the notice post-once per PR/MR whichever end gets there first.

A comment rather than the description on purpose: pr_summary rewrites
descriptions wholesale, so mentions placed there don't survive.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Literal

from src.activities.decorator import activity
from src.runtime.context import RunContext
from src.runtime.notify import READY_MARKER, ready_notice_body

logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False, env=env)


def _gitlab_env(pr_url: str) -> dict:
    host = "gitlab.com"
    if pr_url.startswith("http"):
        try:
            host = pr_url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            host = "gitlab.com"
    return {**os.environ, "GITLAB_HOST": f"https://{host}"}


def _already_notified(
    platform: Literal["github", "gitlab"], repo: str, pr: str, env: dict | None
) -> bool:
    """Whether a ready notice is already on this PR/MR.

    Errs on the side of *not* having notified: a failed lookup returns
    False so a transient API blip can't silently swallow the ping. A
    duplicate is noise; a missed ping is the bug this feature exists to
    fix.
    """
    if platform == "github":
        proc = _run(["gh", "pr", "view", pr, "-R", repo, "--json", "comments"])
        if proc.returncode != 0:
            logger.warning("could not list PR comments for dedupe: %s", proc.stderr.strip())
            return False
        try:
            comments = json.loads(proc.stdout).get("comments", [])
        except (json.JSONDecodeError, AttributeError):
            return False
        return any(READY_MARKER in (c.get("body") or "") for c in comments)

    proc = _run(["glab", "api", "-R", repo, f"projects/:id/merge_requests/{pr}/notes"], env=env)
    if proc.returncode != 0:
        logger.warning("could not list MR notes for dedupe: %s", proc.stderr.strip())
        return False
    try:
        notes = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(notes, list):
        return False
    return any(READY_MARKER in (n.get("body") or "") for n in notes)


@activity(name="Notifying reviewers")
def post_ready_notice(
    platform: Literal["github", "gitlab"],
    repo: str,
    pr: str,
    handles: list[str],
    *,
    pr_url: str = "",
    findings: int = 0,
    ctx: RunContext,  # noqa: ARG001
) -> bool:
    """Post the ping, returning whether a new comment was created.

    False covers every no-op: nobody configured, already notified, or the
    provider rejected the comment. Never raises — a failed notification
    must not fail the workflow that produced a perfectly good MR.
    """
    if not handles or not pr:
        return False

    env = _gitlab_env(pr_url) if platform == "gitlab" else None

    try:
        if _already_notified(platform, repo, pr, env):
            logger.info("ready notice already present on %s#%s, skipping", repo, pr)
            return False

        body = ready_notice_body(handles, findings=findings)
        if platform == "github":
            proc = _run(["gh", "pr", "comment", pr, "-R", repo, "--body", body])
        else:
            proc = _run(
                [
                    "glab",
                    "api",
                    "--method",
                    "POST",
                    "-R",
                    repo,
                    f"projects/:id/merge_requests/{pr}/notes",
                    "-f",
                    f"body={body}",
                ],
                env=env,
            )
    except (subprocess.SubprocessError, OSError):
        logger.warning("ready notice failed on %s#%s", repo, pr, exc_info=True)
        return False

    if proc.returncode != 0:
        logger.warning("ready notice rejected on %s#%s: %s", repo, pr, proc.stderr.strip())
        return False

    logger.info("notified %d user(s) on %s#%s", len(handles), repo, pr)
    return True
