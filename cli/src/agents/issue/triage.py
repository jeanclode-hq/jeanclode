"""TriageAgent — classifies a git issue into one of 7 outcomes."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.issue.base import IssueAgent
from src.agents.issue.schemas import TriageOutput


class TriageAgent(IssueAgent):
    name: ClassVar[str] = "issue-triage"
    prompt_file: ClassVar[str] = "triage.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = TriageOutput
    use_memory: ClassVar[bool] = True
    use_continuity: ClassVar[bool] = True
