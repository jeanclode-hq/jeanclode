from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from src.activities.decorator import activity
from src.activities.review.schemas import (
    Comment,
    GitHubComment,
    GitLabComment,
    PostResult,
    PRRef,
    UnpostedComment,
)
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)

_LGTM = "LGTM 👍"
_BOT_FOLLOWUP = (
    "@jeanclode-bot handle all the comments above — verify each isn't a false positive "
    "or already resolved first. If it's a non-issue or already fixed, resolve the thread "
    "directly. Otherwise, fix it, then resolve the thread."
)

_ErrorType = Literal["out_of_diff", "rate_limited", "permission", "other"]


def _classify(stderr: str) -> _ErrorType:
    text = stderr.lower()
    if (
        "unprocessable entity" in text
        or "http 422" in text
        or "not part of the diff" in text
        or ("pull request review thread" in text and "could not be created" in text)
    ):
        return "out_of_diff"
    if "rate limit" in text or "http 429" in text:
        return "rate_limited"
    if "http 401" in text or "http 403" in text or "must have admin" in text:
        return "permission"
    return "other"


def _run(
    cmd: list[str], *, input_data: str | None = None, timeout: int = 30, env: dict | None = None
):
    return subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _post_github(repo: str, pr: str, comments: list[GitHubComment]) -> PostResult:
    result = PostResult(pr_url=f"https://github.com/{repo}/pull/{pr}")
    inline = [c for c in comments if c.line is not None]
    fallback = [c for c in comments if c.line is None]

    if not comments:
        proc = _run(["gh", "pr", "comment", pr, "-R", repo, "--body", _LGTM])
        if proc.returncode == 0:
            result.lgtm = True
        else:
            result.failed = 1
            result.errors.append(proc.stderr.strip())
        return result

    if inline:
        payload = {
            "event": "COMMENT",
            "comments": [
                {
                    "path": c.path,
                    "line": c.line,
                    "side": (c.side or "RIGHT").upper(),
                    "body": c.body,
                }
                for c in inline
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        try:
            proc = _run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/pulls/{pr}/reviews",
                    "--method",
                    "POST",
                    "--input",
                    tmp_path,
                ],
                timeout=60,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if proc.returncode == 0:
            result.posted += len(inline)
        else:
            result.errors.append(f"batch review failed: {proc.stderr.strip()}")
            for c in inline:
                _retry_single_inline(repo, pr, c, result)

    for c in fallback:
        _post_github_top_level_finding(repo, pr, c, result)

    return result


def _retry_single_inline(repo: str, pr: str, c: GitHubComment, result: PostResult) -> None:
    payload = {
        "event": "COMMENT",
        "comments": [
            {
                "path": c.path,
                "line": c.line,
                "side": (c.side or "RIGHT").upper(),
                "body": c.body,
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name
    try:
        proc = _run(
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "--method",
                "POST",
                "--input",
                tmp_path,
            ],
            timeout=30,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if proc.returncode == 0:
        result.posted += 1
        return
    detail = proc.stderr.strip()
    result.failed += 1
    result.errors.append(f"{c.path}:{c.line} — {detail}")
    result.unposted.append(
        UnpostedComment(
            path=c.path,
            line=c.line,
            body=c.body,
            error_type=_classify(detail),
            error_detail=detail,
        )
    )


def _post_github_top_level_finding(
    repo: str, pr: str, c: GitHubComment, result: PostResult
) -> None:
    proc = _run(["gh", "pr", "comment", pr, "-R", repo, "--body", c.body])
    if proc.returncode == 0:
        result.posted += 1
        return
    detail = proc.stderr.strip()
    result.failed += 1
    result.errors.append(detail)
    result.unposted.append(
        UnpostedComment(
            path=c.path,
            line=None,
            body=c.body,
            error_type=_classify(detail),
            error_detail=detail,
        )
    )


def _gitlab_host_from_url(pr_url: str) -> str:
    if pr_url.startswith("http"):
        try:
            return pr_url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return "gitlab.com"
    return "gitlab.com"


def _fetch_gitlab_diff_refs(repo: str, mr: str, env: dict) -> dict[str, str] | None:
    """Fetch base/head/start SHAs for the MR's latest diff version.

    GitLab's discussions API requires these alongside new_path/new_line to
    anchor a note to a specific diff version — without them it accepts the
    request but can't place the note inline, silently creating a general
    (non-diff) discussion instead.
    """
    proc = _run(
        ["glab", "api", "-R", repo, f"projects/:id/merge_requests/{mr}/versions"],
        timeout=30,
        env=env,
    )
    if proc.returncode != 0:
        return None
    try:
        versions = json.loads(proc.stdout)
        latest = versions[0]
        return {
            "base_sha": latest["base_commit_sha"],
            "head_sha": latest["head_commit_sha"],
            "start_sha": latest["start_commit_sha"],
        }
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def _post_gitlab_thread(repo: str, mr: str, body: str, env: dict) -> subprocess.CompletedProcess:
    """Post a resolvable top-level discussion thread (no position = top-level, resolvable)."""
    return _run(
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
        timeout=30,
        env=env,
    )


def _post_gitlab_note(repo: str, mr: str, body: str, env: dict) -> subprocess.CompletedProcess:
    """Post a plain top-level note — last resort when even thread creation fails."""
    return _run(
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
        timeout=30,
        env=env,
    )


def _post_gitlab(repo: str, mr: str, host: str, comments: list[GitLabComment]) -> PostResult:
    result = PostResult(pr_url=f"https://{host}/{repo}/-/merge_requests/{mr}")
    gl_env = {**os.environ, "GITLAB_HOST": f"https://{host}"}

    if not comments:
        proc = _post_gitlab_note(repo, mr, _LGTM, gl_env)
        if proc.returncode == 0:
            result.lgtm = True
        else:
            result.failed = 1
            result.errors.append(proc.stderr.strip())
        return result

    needs_diff_refs = any(c.new_path and c.new_line for c in comments)
    diff_refs = _fetch_gitlab_diff_refs(repo, mr, gl_env) if needs_diff_refs else None

    for c in comments:
        posted = False
        if c.new_path and c.new_line:
            # `glab api -f "position[base_sha]=..."` never nests into a `position`
            # object — `--field`/`--raw-field` only ever produce flat JSON string
            # keys (glab's own --help: neither parses objects), so GitLab received
            # literal keys like "position[base_sha]" and silently dropped them,
            # creating an unanchored discussion. A real JSON body is required.
            position: dict[str, str | int] = {
                "position_type": "text",
                "new_path": c.new_path,
                "new_line": c.new_line,
            }
            if c.old_path:
                position["old_path"] = c.old_path
            if c.old_line:
                position["old_line"] = c.old_line
            if diff_refs:
                position["base_sha"] = diff_refs["base_sha"]
                position["head_sha"] = diff_refs["head_sha"]
                position["start_sha"] = diff_refs["start_sha"]
            payload = {"body": c.body, "position": position}
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                json.dump(payload, tmp)
                tmp_path = tmp.name
            try:
                proc = _run(
                    [
                        "glab",
                        "api",
                        "--method",
                        "POST",
                        "-R",
                        repo,
                        f"projects/:id/merge_requests/{mr}/discussions",
                        "--input",
                        tmp_path,
                        "-H",
                        "Content-Type: application/json",
                    ],
                    timeout=30,
                    env=gl_env,
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            if proc.returncode == 0:
                result.posted += 1
                posted = True
            else:
                # Inline failed — fall back to resolvable thread, then plain note.
                inline_error = proc.stderr.strip()
                proc = _post_gitlab_thread(repo, mr, c.body, gl_env)
                if proc.returncode != 0:
                    proc = _post_gitlab_note(repo, mr, c.body, gl_env)
                if proc.returncode == 0:
                    result.posted += 1
                    posted = True
                else:
                    result.failed += 1
                    result.errors.append(inline_error)
                    result.unposted.append(
                        UnpostedComment(
                            path=c.new_path or c.old_path or "",
                            line=c.new_line if c.new_line is not None else c.old_line,
                            body=c.body,
                            error_type=_classify(inline_error),
                            error_detail=inline_error,
                        )
                    )
        if not posted and not (c.new_path and c.new_line):
            proc = _post_gitlab_thread(repo, mr, c.body, gl_env)
            if proc.returncode != 0:
                proc = _post_gitlab_note(repo, mr, c.body, gl_env)
            if proc.returncode == 0:
                result.posted += 1
            else:
                detail = proc.stderr.strip()
                result.failed += 1
                result.errors.append(detail)
                result.unposted.append(
                    UnpostedComment(
                        path=c.new_path or c.old_path or "",
                        line=c.new_line if c.new_line is not None else c.old_line,
                        body=c.body,
                        error_type=_classify(detail),
                        error_detail=detail,
                    )
                )

    return result


@activity(name="Posting review comments")
def post_comments(
    comments: list[Comment],
    pr_ref: PRRef,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> PostResult:
    if pr_ref.platform == "github":
        gh_comments = [c for c in comments if isinstance(c, GitHubComment)]
        return _post_github(pr_ref.repo, pr_ref.pr, gh_comments)
    gl_comments = [c for c in comments if isinstance(c, GitLabComment)]
    host = _gitlab_host_from_url(pr_ref.pr_url) if pr_ref.pr_url else "gitlab.com"
    return _post_gitlab(pr_ref.repo, pr_ref.pr, host, gl_comments)


@activity(name="Notifying bot to sweep review comments")
def post_bot_followup(pr_ref: PRRef, *, ctx: RunContext) -> tuple[bool, str]:  # noqa: ARG001
    """Post a single top-level self-mention asking the bot to sweep the review.

    Only ever called for PRs opened by a bot/token account
    (``pr_ref.author_is_bot``); that flag is the loop guard, since the
    backend accepts a mention from any sender. The bot never writes the
    mention anywhere else — the respond planner is forbidden from
    emitting it — so this is the sole self-trigger in the system.
    """
    if pr_ref.platform == "github":
        proc = _run(["gh", "pr", "comment", pr_ref.pr, "-R", pr_ref.repo, "--body", _BOT_FOLLOWUP])
        return proc.returncode == 0, proc.stderr.strip()

    host = _gitlab_host_from_url(pr_ref.pr_url) if pr_ref.pr_url else "gitlab.com"
    gl_env = {**os.environ, "GITLAB_HOST": f"https://{host}"}
    proc = _post_gitlab_thread(pr_ref.repo, pr_ref.pr, _BOT_FOLLOWUP, gl_env)
    if proc.returncode != 0:
        proc = _post_gitlab_note(pr_ref.repo, pr_ref.pr, _BOT_FOLLOWUP, gl_env)
    return proc.returncode == 0, proc.stderr.strip()
