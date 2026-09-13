"""``count_unresolved_threads`` — how many review threads are still open.

The respond workflow already snapshots the branch SHA before a turn so it
can tell deterministically whether the agent pushed. This is the same
trick for discussion state: a turn that resolves the last open thread
without pushing has converged the review loop just as surely as a push
has advanced it, and only a before/after count of provider state can see
that (the agent's own account of what it did is not evidence).

Errors return ``None`` rather than 0 — a failed count must be
indistinguishable from "don't know", so a caller can't read an API blip
as "everything is resolved".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Literal

from src.activities.decorator import activity
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)

_UNRESOLVED_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
"""


def _run(cmd: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False, env=env)


def _github_unresolved(repo: str, pr: str) -> int | None:
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    unresolved = 0
    cursor: str | None = None
    while True:
        # `-f` for String! params, `-F` for the Int! pr — see
        # adaptors/github/context.py, same coercion trap.
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_UNRESOLVED_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"repo={name}",
            "-F",
            f"pr={pr}",
        ]
        if cursor:
            cmd += ["-f", f"after={cursor}"]
        proc = _run(cmd)
        if proc.returncode != 0:
            logger.debug("thread count failed for %s#%s: %s", repo, pr, proc.stderr.strip())
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        conn = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
            "reviewThreads"
        ) or {}
        for node in conn.get("nodes") or []:
            if not node.get("isResolved"):
                unresolved += 1
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return unresolved
        cursor = page.get("endCursor")
        if not cursor:
            return unresolved


def _gitlab_unresolved(repo: str, mr: str, target_url: str) -> int | None:
    host = "gitlab.com"
    if target_url.startswith("http"):
        try:
            host = target_url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            host = "gitlab.com"
    env = {**os.environ, "GITLAB_HOST": f"https://{host}"}
    proc = _run(
        [
            "glab",
            "api",
            "--paginate",
            "-R",
            repo,
            f"projects/:id/merge_requests/{mr}/discussions",
        ],
        env=env,
    )
    if proc.returncode != 0:
        logger.debug("thread count failed for %s!%s: %s", repo, mr, proc.stderr.strip())
        return None
    try:
        discussions = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(discussions, list):
        return None

    unresolved = 0
    for discussion in discussions:
        notes = discussion.get("notes") or []
        # Only resolvable notes carry `resolved`; plain comments omit both
        # and are not threads a reviewer can close, so they don't count.
        if any(n.get("resolvable") and not n.get("resolved") for n in notes):
            unresolved += 1
    return unresolved


@activity(name="Counting open threads")
def count_unresolved_threads(
    platform: Literal["github", "gitlab"],
    repo: str,
    pr: str,
    *,
    target_url: str = "",
    ctx: RunContext,  # noqa: ARG001
) -> int | None:
    """Unresolved review threads on this PR/MR, or None if it can't be read."""
    if not pr or not repo:
        return None
    try:
        if platform == "github":
            return _github_unresolved(repo, pr)
        return _gitlab_unresolved(repo, pr, target_url)
    except (subprocess.SubprocessError, OSError):
        logger.warning("thread count errored for %s#%s", repo, pr, exc_info=True)
        return None
