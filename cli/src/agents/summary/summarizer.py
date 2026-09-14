from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.summary.base import SummaryAgent
from src.agents.summary.schemas import SummaryOutput


class SummarizerInput(BaseModel):
    pr_description: str
    diff: str


class SummarizerAgent(SummaryAgent):
    name: ClassVar[str] = "Summarizer"
    prompt_file: ClassVar[str] = "summarizer.md"
    system_prompt_file: ClassVar[str] = "context.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = SummaryOutput
