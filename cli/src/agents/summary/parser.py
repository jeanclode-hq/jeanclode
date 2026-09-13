from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.summary.base import SummaryAgent
from src.agents.summary.schemas import ParserOutput


class ParserInput(BaseModel):
    draft: str


class ParserAgent(SummaryAgent):
    name: ClassVar[str] = "Parser"
    prompt_file: ClassVar[str] = "parser.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = ParserOutput
