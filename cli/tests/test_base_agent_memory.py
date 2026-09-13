"""BaseAgent — opt-in memory tool wiring (#183).

When ``use_memory`` is True on the agent class AND ``ctx.memory_enabled``
is True (set by runner/run.py from the presence of
``JEANCLODE_MEMORY_API_URL`` in the container's env, per #181):
  - the six ``mcp__memory__*`` tools end up in allowed_tools
  - the in-process memory MCP server is registered under ``mcp_servers``
  - a protocol block framing memory as durable, cross-run knowledge is
    appended to the prompt

Either flag being False must be a complete no-op — mirrors
``tests/test_base_agent_skills.py``'s structure for the analogous
``use_third_party_skills`` gate.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.agents.memory_tool import MEMORY_SERVER_NAME, MEMORY_TOOL_NAMES
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


class _Input(BaseModel):
    message: str


def _make_agent(tmp_path: Path, *, use_memory: bool = False) -> type[BaseAgent]:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "p.md").write_text("Task: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "a"
        prompt_file: ClassVar[str] = str(prompts / "p.md")
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1

    _A.use_memory = use_memory
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


async def _capture(agent_cls: type[BaseAgent], ctx: RunContext) -> tuple[str, Any]:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        captured["options"] = options
        return _stream()

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent_cls().invoke(_Input(message="hi"), ctx)

    return captured["prompt"], captured["options"]


async def test_opt_out_means_no_memory_wiring_even_if_run_has_it_enabled(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_memory=False)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), memory_enabled=True)

    prompt, options = await _capture(Agent, ctx)

    assert "=== Memory ===" not in prompt
    assert not any(t in (options.allowed_tools or []) for t in MEMORY_TOOL_NAMES)
    assert MEMORY_SERVER_NAME not in (options.mcp_servers or {})


async def test_opt_in_with_run_memory_disabled_is_a_no_op(tmp_path: Path) -> None:
    """Agent wants memory, but this run's workspace never opted in — no wiring."""
    Agent = _make_agent(tmp_path, use_memory=True)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), memory_enabled=False)

    prompt, options = await _capture(Agent, ctx)

    assert "=== Memory ===" not in prompt
    assert not any(t in (options.allowed_tools or []) for t in MEMORY_TOOL_NAMES)
    assert MEMORY_SERVER_NAME not in (options.mcp_servers or {})


async def test_opt_in_with_run_memory_enabled_wires_tools_server_and_prompt(
    tmp_path: Path,
) -> None:
    Agent = _make_agent(tmp_path, use_memory=True)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), memory_enabled=True)

    prompt, options = await _capture(Agent, ctx)

    # Prompt: agent's own task + the memory protocol block, framed as
    # durable/cross-run rather than this-run scratch state.
    assert "Task: hi" in prompt
    assert "=== Memory ===" in prompt
    assert "durable" in prompt
    assert "cross-run" in prompt
    assert "scratch space" in prompt

    # Options: all six memory tools authorized + the server registered.
    for tool_name in MEMORY_TOOL_NAMES:
        assert tool_name in (options.allowed_tools or [])
    assert MEMORY_SERVER_NAME in (options.mcp_servers or {})


async def test_memory_and_third_party_skills_can_both_be_wired(tmp_path: Path) -> None:
    """Independent opt-ins don't clobber each other's tool/prompt wiring."""
    Agent = _make_agent(tmp_path, use_memory=True)
    Agent.use_third_party_skills = True
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), memory_enabled=True)

    prompt, options = await _capture(Agent, ctx)

    assert "=== Memory ===" in prompt
    assert MEMORY_TOOL_NAMES[0] in (options.allowed_tools or [])
