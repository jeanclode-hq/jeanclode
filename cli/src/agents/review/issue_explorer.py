from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent
from src.agents.review.schemas import IssueExplorerOutput


class IssueExplorerInput(BaseModel):
    platform: str
    repo: str
    pr_description: str


class IssueExplorerAgent(ReviewAgent):
    name: ClassVar[str] = "IssueExplorer"
    prompt_file: ClassVar[str] = "issue_explorer.md"
    allowed_tools: ClassVar[list[str]] = ["Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = IssueExplorerOutput
