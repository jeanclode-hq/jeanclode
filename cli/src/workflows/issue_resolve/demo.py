"""Demo gate glue for issue-resolve: one demo agent session per round."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.activities.demo import HINTS_FILE, DemoRound
from src.activities.git import WorktreePath
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
