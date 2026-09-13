from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent
from src.agents.review.schemas import FilterResult


class FactCheckerInput(BaseModel):
    pr_description: str
    diff: str
    comments_json: str
    issue_context: str = ""


class FactCheckerAgent(ReviewAgent):
    name: ClassVar[str] = "FactChecker"
    prompt_file: ClassVar[str] = "fact_checker.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = FilterResult
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
