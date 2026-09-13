"""PR/MR-side git activities — open a PR, close it, attach a label."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Literal

from src.activities.decorator import activity
from src.activities.git.schemas import PRRef
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def pr_id(pr: PRRef) -> str:
    """Extract the PR/MR number from its URL (trailing path segment).

    Branch names are deterministic (see issue_branch / branch_name) and
    reused across retries, so by the time a PR/MR exists on that branch
    it may not be the *only* one GitHub/GitLab has ever seen there —
    closed/merged PRs from earlier attempts stick around. Resolving "the
    PR for this branch" ambiently (no explicit id) is ambiguous as soon
    as any history exists, so every command that acts on a specific
    PR/MR must address it by id rather than by branch.

    Also used by ``src.activities.ci_watch`` to address the MR/PR whose
    pipeline it's polling.
    """
    return pr.url.rstrip("/").rsplit("/", 1)[-1]


def pr_number(pr: PRRef) -> int | None:
    """The PR/MR number as an int, or ``None`` if the URL's last segment isn't one.

    The backend keys the ``execution_pull_requests`` link on this number, so
    it's emitted in the structured result rather than left for the backend to
    re-parse out of the URL.
    """
    segment = pr_id(pr)
    return int(segment) if segment.isdigit() else None


def _find_open_pr(
    branch: str, platform: Literal["github", "gitlab"], *, ctx: RunContext
) -> str | None:
    """Return the URL of an already-open PR/MR for ``branch``, if any.

    Branch names are deterministic (see issue_branch / branch_name), so a
    retried execution can find the draft PR a prior failed attempt already
    opened on this same branch instead of erroring on ``pr create`` /
    ``mr create`` or opening an orphaned duplicate. Explicitly filters to
    open ones — the branch may also carry closed/merged PRs/MRs from
    earlier attempts, and those must never be mistaken for a reusable one.
    """
    if platform == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"]
    else:
        cmd = ["glab", "mr", "list", f"--source-branch={branch}", "-F", "json"]
    try:
        proc = subprocess.run(cmd, cwd=ctx.cwd, capture_output=True, text=True, check=False)
    except (subprocess.SubprocessError, OSError):
        logger.debug("Failed to look up existing PR/MR for branch %s", branch, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    try:
        matches = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not matches:
        return None
    first = matches[0]
    return first.get("url") if platform == "github" else first.get("web_url")


@activity(name="Opening PR")
def open_pr(
    branch: str,
    title: str,
    body: str,
    platform: Literal["github", "gitlab"],
    *,
    ctx: RunContext,
) -> PRRef:
    """Create a PR/MR on the current branch and return its URL.

    Reuses an existing open PR/MR on ``branch`` if one is already there.

    Never opens as a draft, even when the PR is created before any real
    commit exists (sentry-fix opens one per target repo up front so the
    live feed has a link early). A draft title makes CI skip the pipeline
    under a `$CI_MERGE_REQUEST_TITLE =~ /^Draft:/ → when: never` workflow
    rule, and un-drafting is not itself a pipeline trigger — the MR would
    then sit blocked on a pipeline that can never be created.
    """
    existing = _find_open_pr(branch, platform, ctx=ctx)
    if existing:
        logger.info("Reusing existing open PR/MR for branch %s: %s", branch, existing)
        return PRRef(url=existing, branch=branch, platform=platform)

    if platform == "github":
        cmd = ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body]
    else:
        # --source-branch is not optional: without it `glab mr create`
        # takes the source from whatever branch the cwd happens to have
        # checked out, so a caller whose ctx.cwd isn't the worktree for
        # `branch` silently opens an MR from the wrong branch instead of
        # failing. Same guarantee `gh pr create --head` gives above.
        cmd = [
            "glab",
            "mr",
            "create",
            f"--source-branch={branch}",
            "--title",
            title,
            "--description",
            body,
        ]

    proc = subprocess.run(cmd, cwd=ctx.cwd, capture_output=True, text=True, check=True)
    return PRRef(url=proc.stdout.strip(), branch=branch, platform=platform)


def _run(cmd: list[str], *, cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = f"{' '.join(cmd)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)


@activity(name="Closing PR")
def close_pr(pr: PRRef, *, ctx: RunContext, comment: str = "") -> None:
    """Close a PR/MR, optionally leaving a comment saying why.

    Used for a draft opened speculatively that turned out to need no
    change — sentry-fix opens one per target repo up front, before the
    fixer runs, so a repo triage over-listed would otherwise be left with
    an empty MR on it.
    """
    the_id = pr_id(pr)
    if comment:
        try:
            _run(
                ["gh", "pr", "comment", the_id, "--body", comment]
                if pr.platform == "github"
                else ["glab", "mr", "note", the_id, "--message", comment],
                cwd=ctx.cwd,
            )
        except RuntimeError:
            logger.warning("Failed to comment before closing %s", pr.url, exc_info=True)
    cmd = (
        ["gh", "pr", "close", the_id]
        if pr.platform == "github"
        else ["glab", "mr", "close", the_id]
    )
    _run(cmd, cwd=ctx.cwd)


@activity(name="Attaching review label")
def attach_label(pr: PRRef, label: str, *, ctx: RunContext) -> None:
    """Attach a single label to the PR/MR, forcing a fresh ``labeled`` webhook.

    ``--add-label``/``--label`` is a silent no-op if the label is already
    present — no state change, no webhook. That's fine the first time (the
    label is genuinely new), but every caller here also re-attaches after
    later pushes to the same PR/MR (retried issue-resolve runs, sentry-fix
    retries), expecting that to re-trigger review/summary the same way a
    fresh label add would (see docs/architecture/autonomous-loop.md and
    ``relabel_pr``, which the respond workflow uses for the same reason).
    Remove-then-add forces that fresh event every time, whether or not the
    label was already there. The remove is best-effort: a provider that
    errors removing an absent label shouldn't block the add that matters.
    """
    the_id = pr_id(pr)
    remove_cmd = (
        ["gh", "pr", "edit", the_id, "--remove-label", label]
        if pr.platform == "github"
        else ["glab", "mr", "update", the_id, "--unlabel", label]
    )
    subprocess.run(remove_cmd, cwd=ctx.cwd, capture_output=True, text=True, check=False)
    add_cmd = (
        ["gh", "pr", "edit", the_id, "--add-label", label]
        if pr.platform == "github"
        else ["glab", "mr", "update", the_id, "--label", label]
    )
    _run(add_cmd, cwd=ctx.cwd)
