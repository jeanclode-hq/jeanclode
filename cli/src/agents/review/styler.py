from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent
from src.agents.review.schemas import StyleResult


class StylerInput(BaseModel):
    comments_json: str


class StylerAgent(ReviewAgent):
    name: ClassVar[str] = "Styler"
    prompt_file: ClassVar[str] = "styler.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = StyleResult
    prefer_small_model: ClassVar[bool] = True
