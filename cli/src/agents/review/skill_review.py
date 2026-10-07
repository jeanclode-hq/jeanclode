from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent


class SkillReviewInput(BaseModel):
    platform: str
    pr_description: str
    diff: str
    issue_context: str = ""
    skill_name: str
    skill_instructions: str


class SkillReviewAgent(ReviewAgent):
    name: ClassVar[str] = "SkillReview"
    prompt_file: ClassVar[str] = "skill_review.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 150
    use_third_party_skills: ClassVar[bool] = False
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
