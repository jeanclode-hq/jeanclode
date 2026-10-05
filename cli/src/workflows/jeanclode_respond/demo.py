"""The demo gate on a respond turn, on the PR/MR the mention came from.

Armed by the planner itself: a ``StructuredOutput`` carrying a ``demo_plan``
runs the same ``require_demo_hook`` issue-resolve uses, against the PR's own
checkout, after the CI gate on the same call. Without one the turn finishes
exactly as before.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import HookContext, HookMatcher, PreToolUseHookInput

from src.activities.demo import (
    DEMO_DIR,
    HINTS_FILE,
    DemoGateState,
    DemoRound,
    fresh_demo_dir,
    split_demo_block,
)
from src.activities.git import WorktreePath
from src.activities.respond import MentionContext
from src.adaptors.diffn import prepare_diff
from src.agents.demo import DemoAgent, DemoInput, DemoOutput
from src.agents.hooks import (
    MAX_DEMO_ROUNDS,
    GateResults,
    forbid_publishing_hook,
    require_demo_hook,
)
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 60_000


class RespondDemo:
    """One respond turn's demo gate: ``state`` and ``plan`` are read back by the runner."""

    def __init__(
        self,
        worktree: WorktreePath,
        starting_sha: str,
        mention: MentionContext,
        pr_url: str,
        pr_description: str,
        diff: str,
        discussions: str,
        *,
        ctx: RunContext,
        demo_dir: Path = DEMO_DIR,
    ) -> None:
        self.state = DemoGateState()
        self.plan = ""
        self.ci_gates = GateResults()
        self._worktree = worktree
        self._starting_sha = starting_sha
        self._mention = mention
        self._pr_url = pr_url
        self._pr_description = split_demo_block(pr_description)[1]
        self._diff = diff
        self._discussions = discussions
        self._ctx = ctx
        self._demo_dir = demo_dir

    def hook(self) -> HookMatcher:
        """Register after every hook ``ci_gates`` tracks."""
        inner = require_demo_hook(
            [self._worktree],
            self._run,
            self.state,
            ctx=self._ctx,
            ci_gates=self.ci_gates,
            demo_dir=self._demo_dir,
        )

        async def _hook(
            hook_input: PreToolUseHookInput, tool_use_id: str | None, context: HookContext
        ) -> dict[str, Any]:
            plan = (hook_input.get("tool_input") or {}).get("demo_plan")
            if not plan:
                return {}
            if not self.plan:
                fresh_demo_dir(self._demo_dir)
            self.plan = str(plan)
            return cast("dict[str, Any]", await inner.hooks[0](hook_input, tool_use_id, context))

        return HookMatcher(matcher=inner.matcher, hooks=[_hook], timeout=inner.timeout)

    def _turn_diff(self) -> str:
        raw = subprocess.run(
            ["git", "diff", self._starting_sha, "HEAD"],
            cwd=self._worktree.path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if not raw.strip():
            return self._diff
        pushed = prepare_diff(raw)
        diff = f"{self._diff}\n\n# Pushed on top of it by this turn\n\n{pushed}"
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + "\n… (diff truncated; read the files in the checkout)"
        return diff

    async def _run(self, round_no: int) -> DemoRound:
        hints = self._demo_dir / HINTS_FILE
        mention = self._mention
        result = await DemoAgent().invoke(
            DemoInput(
                issue_url=self._pr_url,
                issue_body=self._pr_description,
                comments=(
                    f"{self._discussions}\n\n"
                    f"Mention from {mention.mention_author}:\n{mention.mention_body}"
                ).strip(),
                demo_plan=self.plan,
                diff=self._turn_diff(),
                hints=hints.read_text(errors="replace") if hints.is_file() else "",
                repos=[{"name": mention.repo, "path": str(self._worktree.path)}],
                demo_dir=str(self._demo_dir),
                round=round_no,
                max_rounds=MAX_DEMO_ROUNDS,
                source="pr",
            ),
            self._ctx,
            extra_hooks={"PreToolUse": [forbid_publishing_hook()]},
        )
        try:
            output = DemoOutput.model_validate(result.structured)
        except Exception as exc:
            msg = "demo agent returned no valid output"
            raise RuntimeError(msg) from exc
        return DemoRound(**output.model_dump())
