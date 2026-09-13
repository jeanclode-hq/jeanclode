from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent
from src.agents.review.schemas import FilterResult


class DeduplicatorInput(BaseModel):
    discussions: str
    comments_json: str


class DeduplicatorAgent(ReviewAgent):
    name: ClassVar[str] = "Deduplicator"
    prompt_file: ClassVar[str] = "deduplicator.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = FilterResult
    prefer_small_model: ClassVar[bool] = True
