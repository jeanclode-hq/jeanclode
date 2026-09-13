from __future__ import annotations

import os
import subprocess

from src.activities.decorator import activity
from src.activities.review.schemas import (
    PRRef,
    RecoverResult,
    UnpostedComment,
)
from src.runtime.context import RunContext

_REASONS = {
    "out_of_diff": "line outside the PR diff",
    "rate_limited": "rate-limited",
    "permission": "permission denied",
    "other": "inline post failed",
}


def _build_body(items: list[UnpostedComment]) -> str:
    header = (
        "**Findings that could not be posted inline** — "
        "rolling them up here so they aren't lost.\n\n---"
    )
    blocks: list[str] = []
    for c in items:
        if c.path and c.line:
            anchor = f"`{c.path}:{c.line}`"
        elif c.path:
            anchor = f"`{c.path}`"
        else:
            anchor = ""
        reason = _REASONS.get(c.error_type, c.error_type)
        meta = f"_{reason}_" if not anchor else f"{anchor} · _{reason}_"
        prefix = f"**{meta}**\n\n"
        blocks.append(prefix + c.body)
    return header + "\n\n" + "\n\n---\n\n".join(blocks)


def _run(
    cmd: list[str], *, timeout: int = 60, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _gitlab_host_from_url(pr_url: str) -> str:
    if pr_url.startswith("http"):
        try:
            return pr_url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return "gitlab.com"
    return "gitlab.com"


def _post_github(repo: str, pr: str, body: str) -> tuple[bool, str]:
    proc = _run(["gh", "pr", "comment", pr, "-R", repo, "--body", body])
    return proc.returncode == 0, proc.stderr.strip()


def _post_gitlab(repo: str, mr: str, body: str, host: str) -> tuple[bool, str]:
    gl_env = {**os.environ, "GITLAB_HOST": f"https://{host}"}
    # Try resolvable thread first, plain note as last resort.
    proc = _run(
        [
            "glab",
            "api",
            "--method",
            "POST",
            "-R",
            repo,
            f"projects/:id/merge_requests/{mr}/discussions",
            "-f",
            f"body={body}",
        ],
        env=gl_env,
    )
    if proc.returncode != 0:
        proc = _run(
            [
                "glab",
                "api",
                "--method",
                "POST",
                "-R",
                repo,
                f"projects/:id/merge_requests/{mr}/notes",
                "-f",
                f"body={body}",
            ],
            env=gl_env,
        )
    return proc.returncode == 0, proc.stderr.strip()


@activity(name="Recovering unposted findings")
def recover_failed_post(
    unposted: list[UnpostedComment],
    pr_ref: PRRef,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> RecoverResult:
    if pr_ref.platform == "github":
        pr_url = f"https://github.com/{pr_ref.repo}/pull/{pr_ref.pr}"
        host = ""
    else:
        host = _gitlab_host_from_url(pr_ref.pr_url) if pr_ref.pr_url else "gitlab.com"
        pr_url = f"https://{host}/{pr_ref.repo}/-/merge_requests/{pr_ref.pr}"

    result = RecoverResult(pr_url=pr_url)
    if not unposted:
        return result

    body = _build_body(unposted)
    if pr_ref.platform == "github":
        ok, err = _post_github(pr_ref.repo, pr_ref.pr, body)
    else:
        ok, err = _post_gitlab(pr_ref.repo, pr_ref.pr, body, host)

    if ok:
        result.recovered = len(unposted)
    else:
        result.error = err

    return result
