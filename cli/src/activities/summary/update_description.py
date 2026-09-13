"""Overwrite the PR/MR description with the rendered summary."""

from __future__ import annotations

import os
import subprocess

from src.activities.decorator import activity
from src.activities.summary.schemas import PostResult, PRSnapshot, SummaryPayload
from src.runtime.context import RunContext


def _run(
    cmd: list[str], *, timeout: int = 30, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _gitlab_host_from_url(pr_url: str) -> str:
    if pr_url.startswith("http"):
        try:
            return pr_url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return "gitlab.com"
    return "gitlab.com"


@activity(name="Updating PR description")
def update_pr_description(
    payload: SummaryPayload,
    snapshot: PRSnapshot,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> PostResult:
    if snapshot.platform == "github":
        url = f"https://github.com/{snapshot.repo}/pull/{snapshot.pr}"
        proc = _run(
            [
                "gh",
                "pr",
                "edit",
                snapshot.pr,
                "-R",
                snapshot.repo,
                "--body",
                payload.body,
            ]
        )
    else:
        host = _gitlab_host_from_url(snapshot.pr_url) if snapshot.pr_url else "gitlab.com"
        url = f"https://{host}/{snapshot.repo}/-/merge_requests/{snapshot.pr}"
        gl_env = {**os.environ, "GITLAB_HOST": f"https://{host}"}
        proc = _run(
            [
                "glab",
                "mr",
                "update",
                snapshot.pr,
                "-R",
                snapshot.repo,
                "--description",
                payload.body,
            ],
            env=gl_env,
        )

    return PostResult(
        posted=proc.returncode == 0,
        pr_url=url,
        error=proc.stderr.strip() if proc.returncode != 0 else "",
    )
