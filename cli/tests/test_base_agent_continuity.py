"""BaseAgent — opt-in continuity block wiring.

When ``use_continuity`` is True on the agent class, a block warning that
the thread history in the prompt may include prior turns from other
jeanclode agents/workflows is appended — no run-context flag involved,
unlike ``use_memory``, since this is pure prompt framing with no tools or
external service attached. ``use_continuity = False`` must be a complete
no-op — mirrors ``tests/test_base_agent_memory.py``'s structure for the
analogous ``use_memory`` gate.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


class _Input(BaseModel):
    message: str


def _make_agent(tmp_path: Path, *, use_continuity: bool = False) -> type[BaseAgent]:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "p.md").write_text("Task: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "a"
        prompt_file: ClassVar[str] = str(prompts / "p.md")
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1

    _A.use_continuity = use_continuity
    return _A


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result="ok",
    )


def _stream() -> Any:
    async def gen() -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield _result_message()

    return gen()


async def _capture(agent_cls: type[BaseAgent], ctx: RunContext) -> str:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        return _stream()

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent_cls().invoke(_Input(message="hi"), ctx)

    return captured["prompt"]


async def test_opt_out_means_no_continuity_block(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_continuity=False)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())

    prompt = await _capture(Agent, ctx)

    assert "=== Continuity ===" not in prompt


async def test_opt_in_appends_continuity_block_after_task_prompt(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_continuity=True)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())

    prompt = await _capture(Agent, ctx)

    assert "Task: hi" in prompt
    assert "=== Continuity ===" in prompt
    assert prompt.index("Task: hi") < prompt.index("=== Continuity ===")
