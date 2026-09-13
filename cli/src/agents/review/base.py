from __future__ import annotations

from pathlib import Path

from src.agents.base import BaseAgent

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "review"


class ReviewAgent(BaseAgent):
    def _prompts_dir(self) -> Path:
        return _PROMPTS_DIR
