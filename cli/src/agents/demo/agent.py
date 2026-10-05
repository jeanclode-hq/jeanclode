"""DemoAgent — starts the app with fake data and records the change working."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.agents.demo.schemas import DemoOutput

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "demo"


class DemoAgent(BaseAgent):
    name: ClassVar[str] = "demo"
    prompt_file: ClassVar[str] = "demo.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    max_turns: ClassVar[int] = 120
    output_schema: ClassVar[type[BaseModel] | None] = DemoOutput
    use_third_party_skills: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True

    def _prompts_dir(self) -> Path:
        return _PROMPTS_DIR
