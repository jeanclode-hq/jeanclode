"""``relabel_pr`` activity — force re-attach a ``jeanclode:*`` label.

Removes-then-adds so the webhook re-fires even when the label was
already attached. ``gh pr edit --add-label`` is silently a no-op when
the label exists, which would skip the re-trigger we want.
"""

from __future__ import annotations

import os
import subprocess

from src.activities.decorator import activity
from src.activities.respond.schemas import MentionContext, RelabelPRResult
from src.runtime.context import RunContext


def _run(
    cmd: list[str], *, timeout: int = 30, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _gitlab_host_from_url(url: str) -> str:
    if url.startswith("http"):
        return url.split("//", 1)[1].split("/", 1)[0]
    return "gitlab.com"


@activity(name="Re-attaching label")
def relabel_pr(
    label: str,
    mention: MentionContext,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> RelabelPRResult:
    if not mention.pr:
        return RelabelPRResult(error="missing pr in mention context")
    if mention.platform == "github":
        _run(["gh", "pr", "edit", mention.pr, "-R", mention.repo, "--remove-label", label])
        proc = _run(["gh", "pr", "edit", mention.pr, "-R", mention.repo, "--add-label", label])
    else:
        host = _gitlab_host_from_url(mention.target_url)
        gl_env = {**os.environ, "GITLAB_HOST": f"https://{host}"}
        _run(
            ["glab", "mr", "update", mention.pr, "-R", mention.repo, "--unlabel", label],
            env=gl_env,
        )
        proc = _run(
            ["glab", "mr", "update", mention.pr, "-R", mention.repo, "--label", label],
            env=gl_env,
        )
    return RelabelPRResult(
        relabeled=proc.returncode == 0,
        error=proc.stderr.strip() if proc.returncode != 0 else "",
    )
