"""BaseAgent — opt-in org MCP connector wiring (#191).

When ``use_mcp_connectors`` is True and the run has org-registered MCP
servers:
  - each server is registered under ``ClaudeAgentOptions.mcp_servers``
  - a ``mcp__<name>__*`` wildcard is added to allowed_tools so the agent
    can actually call the server's tools
  - it coexists with the in-process memory server, which uses the same
    ``mcp_servers`` dict
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.agents.memory_tool import MEMORY_SERVER_NAME
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.mcp_connectors import McpServerSpec


class _Input(BaseModel):
    message: str


def _make_agent(
    tmp_path: Path,
    *,
    use_mcp_connectors: bool = False,
    use_memory: bool = False,
) -> type[BaseAgent]:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "p.md").write_text("Task: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "a"
        prompt_file: ClassVar[str] = str(prompts / "p.md")
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1

    _A.use_mcp_connectors = use_mcp_connectors
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


async def _capture(agent_cls: type[BaseAgent], ctx: RunContext) -> Any:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["options"] = options
        return _stream()

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent_cls().invoke(_Input(message="hi"), ctx)

    return captured["options"]


async def test_opt_out_means_no_mcp_wiring(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    server = McpServerSpec(name="outline", url="https://mcp.outline.com/sse")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), mcp_servers=[server])

    options = await _capture(Agent, ctx)

    assert "outline" not in (options.mcp_servers or {})
    assert "mcp__outline__*" not in (options.allowed_tools or [])


async def test_opt_in_with_no_servers_is_no_op(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_mcp_connectors=True)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())

    options = await _capture(Agent, ctx)

    assert not (options.mcp_servers or {})


async def test_opt_in_registers_server_and_wildcard_tool(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_mcp_connectors=True)
    server = McpServerSpec(
        name="outline",
        url="https://mcp.outline.com/sse",
        header="Authorization",
        auth_scheme="bearer",
    )
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), mcp_servers=[server])

    options = await _capture(Agent, ctx)

    assert "outline" in options.mcp_servers
    config = options.mcp_servers["outline"]
    assert config["type"] == "http"
    assert config["url"] == "https://mcp.outline.com/sse"
    assert config["headers"]["Authorization"].startswith("Bearer ")
    assert "mcp__outline__*" in options.allowed_tools


async def test_unauthenticated_server_has_no_headers(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_mcp_connectors=True)
    server = McpServerSpec(name="public-tool", url="https://mcp.public.example.com/sse")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), mcp_servers=[server])

    options = await _capture(Agent, ctx)

    assert "headers" not in options.mcp_servers["public-tool"]


async def test_multiple_servers_all_registered(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_mcp_connectors=True)
    s1 = McpServerSpec(name="one", url="https://one.example.com/sse")
    s2 = McpServerSpec(name="two", url="https://two.example.com/sse")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), mcp_servers=[s1, s2])

    options = await _capture(Agent, ctx)

    assert set(options.mcp_servers) == {"one", "two"}
    assert "mcp__one__*" in options.allowed_tools
    assert "mcp__two__*" in options.allowed_tools


async def test_mcp_connectors_coexist_with_memory_server(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_mcp_connectors=True, use_memory=True)
    server = McpServerSpec(name="outline", url="https://mcp.outline.com/sse")
    ctx = RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        mcp_servers=[server],
        memory_enabled=True,
    )

    options = await _capture(Agent, ctx)

    assert MEMORY_SERVER_NAME in options.mcp_servers
    assert "outline" in options.mcp_servers


async def test_org_mcp_server_named_memory_does_not_overwrite_builtin_memory_server(
    tmp_path: Path,
) -> None:
    """Regression test: the backend rejects "memory" as an MCP server name
    on create/update, but a pre-existing row (or any other path that
    slipped one through) must not silently overwrite the in-process
    memory server the agent actually relies on."""
    Agent = _make_agent(tmp_path, use_mcp_connectors=True, use_memory=True)
    rogue = McpServerSpec(name=MEMORY_SERVER_NAME, url="https://attacker.example.com/sse")
    ctx = RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        mcp_servers=[rogue],
        memory_enabled=True,
    )

    options = await _capture(Agent, ctx)

    # The built-in memory server wins — the org entry is dropped, not merged.
    config = options.mcp_servers[MEMORY_SERVER_NAME]
    assert config.get("url") != "https://attacker.example.com/sse"
    assert f"mcp__{MEMORY_SERVER_NAME}__*" not in (options.allowed_tools or [])
