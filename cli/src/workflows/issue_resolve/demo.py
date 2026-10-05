"""Demo gate glue for issue-resolve: run the demo agent, then post its recording."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.activities.demo import (
    HINTS_FILE,
    DemoGateState,
    DemoRound,
    link_demo,
    post_demo_note,
    publish_demo,
)
from src.activities.git import PRRef, WorktreePath
from src.activities.issue import IssueContext
from src.adaptors.diffn import prepare_diff
from src.agents.demo import DemoAgent, DemoInput
from src.agents.hooks import forbid_publishing_hook
from src.agents.issue.schemas import TriageOutput
from src.runtime.context import RunContext
from src.workflows.issue_resolve.utils import parse_demo_output

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 60_000


def _fix_diff(worktrees: dict[str, WorktreePath]) -> str:
    parts = []
    for wt in worktrees.values():
        raw = subprocess.run(
            ["git", "diff", wt.placeholder_sha, "HEAD"],
            cwd=wt.path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if raw.strip():
            parts.append(prepare_diff(raw))
    diff = "\n".join(parts)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n… (diff truncated; read the files in the checkout)"
    return diff


def make_run_demo(
    worktrees: dict[str, WorktreePath],
    issue_url: str,
    issue_ctx: IssueContext,
    triage_output: TriageOutput,
    demo_dir: Path,
    max_rounds: int,
    *,
    ctx: RunContext,
) -> Callable[[int], Awaitable[DemoRound]]:
    """The demo gate's ``run_demo``: one DemoAgent session per round.

    ``ctx`` is the run's default, never the fixer's retargeted LLM context.
    """

    async def _run(round_no: int) -> DemoRound:
        hints = demo_dir / HINTS_FILE
        result = await DemoAgent().invoke(
            DemoInput(
                issue_url=issue_url,
                issue_title=issue_ctx.issue_title,
                issue_body=issue_ctx.issue_body,
                comments=issue_ctx.comments,
                findings=triage_output.findings,
                demo_plan=triage_output.demo_plan or "",
                diff=_fix_diff(worktrees),
                hints=hints.read_text(errors="replace") if hints.is_file() else "",
                repos=[{"name": n, "path": str(wt.path)} for n, wt in worktrees.items()],
                demo_dir=str(demo_dir),
                round=round_no,
                max_rounds=max_rounds,
            ),
            ctx,
            extra_hooks={"PreToolUse": [forbid_publishing_hook()]},
        )
        output = parse_demo_output(result)
        if output is None:
            msg = "demo agent returned no valid output"
            raise RuntimeError(msg)
        return DemoRound(**output.model_dump())

    return _run


def demo_note(state: DemoGateState) -> str:
    """Why a PR/MR carries no demo, for its comment thread."""
    if state.bypass_reason:
        return f"No demo for this change: the fixer stood the demo gate down. {state.bypass_reason}"
    if state.dirty:
        return (
            f"No demo for this change: after {state.rounds} round(s) the fixer's checkout "
            f"still had uncommitted changes, so the demo couldn't run: {', '.join(state.dirty)}."
        )
    if state.error:
        return (
            f"No demo for this change: after {state.rounds} round(s) the demo agent "
            f"failed.\n\n{state.error}"
        )
    if state.last is not None:
        return (
            f"No demo for this change: after {state.rounds} round(s) the demo agent's last "
            f"verdict was `{state.last.verdict}`.\n\n{state.last.evidence.strip()}"
        )
    return ""


def _app_prs(prs: dict[str, PRRef], app_repo: str) -> list[str]:
    """The PRs that get the video: the app's own, or all of them when it has none."""
    return [app_repo] if app_repo in prs else list(prs)


def post_demos(
    prs: dict[str, PRRef], state: DemoGateState, *, ctx: RunContext, cwds: dict[str, Path]
) -> dict[str, str]:
    """Post the last ``ok`` round on the app's PR, a link to it on the others, or
    a note saying why there's no demo.

    Returns each PR's outcome for the workflow result. Never raises: a demo that
    fails to post must not hold back the PRs' labels.
    """
    demo = state.ok
    outcomes: dict[str, str] = {}
    if demo is not None and Path(demo.video_path).is_file():
        video_on = _app_prs(prs, demo.app_repo)
        for name in video_on:
            outcomes[name] = _attempt(
                prs[name], lambda pr, c: publish_demo(pr, demo, ctx=c), "posted", ctx, cwds[name]
            )
        posted = [n for n in video_on if outcomes[n] == "posted"]
        app_pr = prs[posted[0]] if posted else None
        for name in (n for n in prs if n not in video_on):
            if app_pr is None:
                outcomes[name] = "none"
                continue
            outcomes[name] = _attempt(
                prs[name],
                _linker(app_pr),
                "linked",
                ctx,
                cwds[name],
            )
        return outcomes

    note = demo_note(state)
    if demo is not None:
        note = "The demo agent saw the change working but saved no recording."
    last_app = (demo or state.last or DemoRound(verdict="ok")).app_repo
    note_on = _app_prs(prs, last_app)
    for name in prs:
        if note and name in note_on:
            failed = _attempt(
                prs[name], lambda pr, c: post_demo_note(pr, note, ctx=c), "", ctx, cwds[name]
            )
            if failed:
                outcomes[name] = failed
                continue
        outcomes[name] = "bypassed" if state.bypass_reason else "none"
    return outcomes


def _linker(app_pr: PRRef) -> Callable[[PRRef, RunContext], None]:
    def _link(pr: PRRef, ctx: RunContext) -> None:
        link_demo(pr, app_pr, ctx=ctx)

    return _link


def _attempt(
    pr: PRRef,
    action: Callable[[PRRef, RunContext], object],
    done: str,
    ctx: RunContext,
    cwd: Path,
) -> str:
    try:
        action(pr, ctx.with_cwd(cwd))
    except Exception:
        logger.warning("posting the demo on %s failed", pr.url, exc_info=True)
        return "post_failed"
    return done
