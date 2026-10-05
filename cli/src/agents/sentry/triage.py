"""Triage agent — classifies whether a Sentry issue is an actionable code bug."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.activities.sentry.schemas import TriageOutput
from src.agents.sentry.base import SentryAgent


class TriageInput(BaseModel):
    """Render-time input for the triage prompt."""

    issue_id: str
    sentry_url: str
    formatted: str


class TriageAgent(SentryAgent):
    name: ClassVar[str] = "triage"
    prompt_file: ClassVar[str] = "triage.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = TriageOutput
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
