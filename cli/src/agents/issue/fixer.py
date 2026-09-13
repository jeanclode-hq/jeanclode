"""IssueFixerAgent — implements the planned fix and updates the draft PR."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.issue.base import IssueAgent
from src.agents.issue.schemas import IssueFixerOutput


class IssueFixerAgent(IssueAgent):
    name: ClassVar[str] = "issue-fixer"
    prompt_file: ClassVar[str] = "fixer.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = IssueFixerOutput
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
    use_continuity: ClassVar[bool] = True
