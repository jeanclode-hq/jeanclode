from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.review.base import ReviewAgent


class SynthesizerInput(BaseModel):
    platform: str
    pr_description: str
    diff: str
    findings_json: str
    issue_context: str = ""


class SynthesizerAgent(ReviewAgent):
    name: ClassVar[str] = "Synthesizer"
    prompt_file: ClassVar[str] = "synthesizer.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 150
    prefer_small_model: ClassVar[bool] = True
