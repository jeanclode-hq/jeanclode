"""MemoryCuratorAgent — prunes, merges and rewrites a workspace's memory entries."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from src.agents.base import BaseAgent

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "memory"


class MemoryCuratorInput(BaseModel):
    paths: list[str]


class MemoryCuratorAgent(BaseAgent):
    name: ClassVar[str] = "memory-curator"
    prompt_file: ClassVar[str] = "curator.md"
    builtin_tools: ClassVar[list[str] | None] = []
    use_memory: ClassVar[bool] = True
    max_turns: ClassVar[int] = 200

    def _prompts_dir(self) -> Path:
        return _PROMPTS_DIR
