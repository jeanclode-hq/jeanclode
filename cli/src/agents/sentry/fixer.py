"""Fixer agent — implement triage's findings and push to the draft PR(s)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from src.agents.sentry.base import SentryAgent


class FixerInput(BaseModel):
    """Render-time input — triage's findings plus the draft PR / branch refs."""

    sentry_urls: list[str]
    branch: str
    # Triage's investigation notes for every issue in this group. There is
    # no separate planner: triage read the source to reach its verdict, so
    # this is that work, handed over rather than re-derived.
    findings: str
    # Every repo this fix targets, each already a worktree on ``branch``
    # with its draft PR/MR open: {"name", "path", "pr_url",
    # "ci_bypass_path"}. When there's exactly one, cwd IS that worktree;
    # with more than one, cwd is their shared parent.
    repos: list[dict[str, str]] = Field(default_factory=list)


class FixerOutput(BaseModel):
    """JSON the fixer agent emits."""

    changes_summary: str = ""
    static_check_passed: bool = False


class FixerAgent(SentryAgent):
    name: ClassVar[str] = "fixer"
    prompt_file: ClassVar[str] = "fixer.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = FixerOutput
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
