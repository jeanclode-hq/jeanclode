"""IssueFixerAgent — implements the planned fix and updates the draft PR."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.issue.base import IssueAgent
from src.agents.issue.schemas import IssueFixerInput, IssueFixerOutput


class IssueFixerAgent(IssueAgent):
    name: ClassVar[str] = "issue-fixer"
    prompt_file: ClassVar[str] = "fixer.md"
    answer_prompt_file: ClassVar[str] = "fixer_answer.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 250
    output_schema: ClassVar[type[BaseModel] | None] = IssueFixerOutput
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
    use_continuity: ClassVar[bool] = True

    def _render(self, agent_input: BaseModel) -> str:
        if isinstance(agent_input, IssueFixerInput) and not agent_input.code_change:
            return self._render_file(self.answer_prompt_file, agent_input)
        return super()._render(agent_input)
