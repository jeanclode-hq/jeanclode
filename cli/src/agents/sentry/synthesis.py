"""Synthesis agent — groups actionable issues by shared root cause."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.activities.sentry.schemas import SynthesisAgentOutput, TriagedIssue
from src.agents.sentry.base import SentryAgent


class SynthesisInput(BaseModel):
    """Render-time input — the actionable triage records to group."""

    actionable: list[TriagedIssue]


class SynthesisAgent(SentryAgent):
    name: ClassVar[str] = "synthesis"
    prompt_file: ClassVar[str] = "synthesis.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = SynthesisAgentOutput
