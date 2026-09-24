"""BaseAgent.invoke — prompt rendering, emission, output_schema."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, Literal
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import AgentEnd, AgentStart, Event, ToolCall, ToolResult


class _Input(BaseModel):
    message: str


class _Out(BaseModel):
    answer: str


def _make_agent(tmp_path: Path, *, with_schema: bool = False) -> type[BaseAgent]:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "echo.md").write_text("Echo: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "echoer"
        prompt_file: ClassVar[str] = str(prompts / "echo.md")  # absolute
        allowed_tools: ClassVar[list[str]] = []
        max_turns: ClassVar[int] = 1
        output_schema: ClassVar[type[BaseModel] | None] = _Out if with_schema else None

    return _A


def _ctx(tmp_path: Path) -> tuple[RunContext, list[Event]]:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    return (
        RunContext(cwd=tmp_path, workspace=tmp_path, events=bus),
        received,
    )


def _stream(messages: list[Any]) -> Any:
    async def gen() -> AsyncIterator[Any]:
        for m in messages:
            await asyncio.sleep(0)
            yield m

    return gen()


def _result_message(text: str = "ok", structured: Any = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=12,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 2},
        result=text,
        structured_output=structured,
    )


async def test_invoke_renders_prompt_and_emits_lifecycle(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    ctx, received = _ctx(tmp_path)

    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        captured["options"] = options
        tool = ToolUseBlock(id="t1", name="Read", input={"file_path": "/a"})
        am = AssistantMessage(content=[tool], model="claude")
        um = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="contents")])
        return _stream([am, um, _result_message("hello")])

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="hi"), ctx)

    assert captured["prompt"] == "Echo: hi"
    assert result.text == "hello"
    kinds = [type(e) for e in received]
    assert kinds[0] is AgentStart
    assert ToolCall in kinds
    assert ToolResult in kinds
    assert kinds[-1] is AgentEnd


async def test_output_schema_propagates_to_options(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, with_schema=True)
    ctx, _ = _ctx(tmp_path)

    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["options"] = options
        return _stream([_result_message("ok", structured={"answer": "yes"})])

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="x"), ctx)

    output_format = getattr(captured["options"], "output_format", None)
    assert output_format is not None
    assert output_format["type"] == "json_schema"
    assert "properties" in output_format["schema"]
    assert result.structured == {"answer": "yes"}


async def test_prefer_small_model_uses_small_model_from_context(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "echo.md").write_text("Echo: {{ message }}")

    class _Small(BaseAgent):
        name: ClassVar[str] = "small"
        prompt_file: ClassVar[str] = str(prompts / "echo.md")
        allowed_tools: ClassVar[list[str]] = []
        max_turns: ClassVar[int] = 1
        prefer_small_model: ClassVar[bool] = True

    ctx, _ = _ctx(tmp_path)
    ctx = ctx.model_copy(update={"small_model": "haiku-4-5"})
    captured: dict[str, object] = {}

    def fake_query(*, prompt: str, options: object) -> object:
        captured["model"] = getattr(options, "model", None)
        return _stream([_result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query):
        await _Small().invoke(_Input(message="x"), ctx)

    assert captured["model"] == "haiku-4-5"


async def test_prefer_small_model_falls_back_to_haiku(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "echo.md").write_text("Echo: {{ message }}")

    class _Small(BaseAgent):
        name: ClassVar[str] = "small"
        prompt_file: ClassVar[str] = str(prompts / "echo.md")
        allowed_tools: ClassVar[list[str]] = []
        max_turns: ClassVar[int] = 1
        prefer_small_model: ClassVar[bool] = True

    ctx, _ = _ctx(tmp_path)
    captured: dict[str, object] = {}

    def fake_query(*, prompt: str, options: object) -> object:
        captured["model"] = getattr(options, "model", None)
        return _stream([_result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query):
        await _Small().invoke(_Input(message="x"), ctx)

    assert captured["model"] == "haiku"


async def test_prefer_small_model_false_uses_main_model(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    ctx, _ = _ctx(tmp_path)
    ctx = ctx.model_copy(update={"model": "opus", "small_model": "haiku"})
    captured: dict[str, object] = {}

    def fake_query(*, prompt: str, options: object) -> object:
        captured["model"] = getattr(options, "model", None)
        return _stream([_result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query):
        await Agent().invoke(_Input(message="x"), ctx)

    assert captured["model"] == "opus"


async def test_extra_hooks_propagate_to_options(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    ctx, _ = _ctx(tmp_path)

    async def _stop_hook(_input: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        return {}

    matcher = HookMatcher(hooks=[_stop_hook])
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["options"] = options
        return _stream([_result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query):
        await Agent().invoke(_Input(message="x"), ctx, extra_hooks={"Stop": [matcher]})

    assert captured["options"].hooks == {"Stop": [matcher]}


async def test_invoke_without_extra_hooks_leaves_hooks_unset(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    ctx, _ = _ctx(tmp_path)
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["options"] = options
        return _stream([_result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query):
        await Agent().invoke(_Input(message="x"), ctx)

    assert getattr(captured["options"], "hooks", None) is None


async def test_session_init_logs_loaded_plugins_skills_and_servers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    Agent = _make_agent(tmp_path)
    ctx, _ = _ctx(tmp_path)
    init = SystemMessage(
        subtype="init",
        data={
            "model": "claude-x",
            "tools": ["Read", "Bash", "Skill"],
            "plugins": [{"name": "figma", "path": "/p/figma"}],
            "skills": ["figma-context", "email-html"],
            "mcp_servers": [{"name": "memory", "status": "connected"}],
        },
    )

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream([init, _result_message("ok")])

    with patch("src.agents.base.query", side_effect=fake_query), caplog.at_level("INFO"):
        await Agent().invoke(_Input(message="x"), ctx)

    line = next(r.getMessage() for r in caplog.records if "session init" in r.getMessage())
    assert "plugins=[figma]" in line
    assert "skills=[figma-context, email-html]" in line
    assert "mcp=[memory=connected]" in line
    assert "tools=3" in line


async def test_invoke_emits_failed_end_on_exception(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    ctx, received = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> Any:
        async def gen() -> AsyncIterator[Any]:
            raise RuntimeError("upstream boom")
            yield  # unreachable, makes this an async generator

        return gen()

    with (
        patch("src.agents.base.query", side_effect=fake_query),
        pytest.raises(RuntimeError, match="upstream boom"),
    ):
        await Agent().invoke(_Input(message="x"), ctx)

    end = received[-1]
    assert isinstance(end, AgentEnd)
    assert end.ok is False


async def test_structured_output_recovered_from_text_when_payload_is_unrecognised(
    tmp_path: Path,
) -> None:
    """The CLI's --json-schema declares no required properties, so a payload
    whose keys don't match it validates there and reaches us looking fine,
    then silently becomes an all-defaults instance. The same JSON is in the
    result text — prefer it (jc-sentry-1887773)."""
    Agent = _make_agent(tmp_path, with_schema=True)
    ctx, _ = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream(
            [_result_message(text='{"answer": "real"}', structured={"unexpected": "shape"})]
        )

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="x"), ctx)
    assert result.structured == {"answer": "real"}


async def test_matching_structured_output_wins_over_text(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, with_schema=True)
    ctx, _ = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream(
            [_result_message(text='{"answer": "from-text"}', structured={"answer": "ok"})]
        )

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="x"), ctx)
    assert result.structured == {"answer": "ok"}


async def test_unparseable_text_leaves_structured_empty(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, with_schema=True)
    ctx, _ = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream([_result_message(text="I could not do it", structured=None)])

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="x"), ctx)
    assert result.structured is None


# ── the schema we hand the CLI ──────────────────────────────────────────


class _Nested(BaseModel):
    label: str


class _WithRef(BaseModel):
    items: list[_Nested] = []


class _Strictable(BaseModel):
    kind: Literal["a", "b"] = "a"
    note: str = ""
    tags: list[str] = []
    link: str | None = None


def test_schema_for_cli_matches_the_strict_dialect() -> None:
    """The CLI only derives a generation-constraining strict schema when
    every keyword is in its allowed set and each object declares `required`
    plus `additionalProperties: false`. Pydantic's `default` keys break
    that, dropping it to a permissive check where any object validates
    (jc-sentry-1887793)."""
    schema = BaseAgent._schema_for_cli(_Strictable)
    allowed = BaseAgent._STRICT_KEYWORDS
    assert schema["required"] == ["kind", "note", "tags", "link"]
    assert schema["additionalProperties"] is False
    assert set(schema) <= allowed
    for prop in schema["properties"].values():
        assert set(prop) <= allowed, prop
    # property names must survive the keyword filter
    assert set(schema["properties"]) == {"kind", "note", "tags", "link"}
    assert schema["properties"]["kind"]["enum"] == ["a", "b"]
    assert schema["properties"]["link"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_schema_with_refs_is_sent_unmodified() -> None:
    """Dropping $ref/$defs would rewrite the contract, not just tidy it —
    hand those over as-is and let the CLI fall back."""
    schema = BaseAgent._schema_for_cli(_WithRef)
    assert "$defs" in schema
    assert schema == _WithRef.model_json_schema()


def test_envelope_payload_is_unwrapped() -> None:
    fields = {"kind", "note"}
    assert BaseAgent._unwrap_envelope({"input": {"kind": "a"}}, fields) == {"kind": "a"}


def test_single_field_payload_is_not_mistaken_for_an_envelope() -> None:
    fields = {"kind", "note"}
    assert BaseAgent._unwrap_envelope({"kind": "a"}, fields) is None
    assert BaseAgent._unwrap_envelope({"wrapper": {"unrelated": 1}}, fields) is None


async def test_wrapped_structured_output_is_recovered(tmp_path: Path) -> None:
    """Prod's actual failure: the whole verdict nested under "input", which
    a permissive schema validates and reports as a success."""
    Agent = _make_agent(tmp_path, with_schema=True)
    ctx, _ = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream([_result_message(text="", structured={"input": {"answer": "real"}})])

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await Agent().invoke(_Input(message="x"), ctx)
    assert result.structured == {"answer": "real"}
