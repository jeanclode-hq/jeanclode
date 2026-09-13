"""Common base for respond agents.

Each agent's prompt template lives under ``cli/src/prompts/respond/``;
override ``_prompts_dir`` so subclasses only need to set ``prompt_file``
to the bare filename.
"""

from __future__ import annotations

from pathlib import Path

from src.agents.base import BaseAgent

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "respond"


class RespondAgent(BaseAgent):
    """BaseAgent variant that resolves prompts from ``cli/src/prompts/respond/``."""

    def _prompts_dir(self) -> Path:
        return _PROMPTS_DIR
