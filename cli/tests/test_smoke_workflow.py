"""End-to-end smoke: EchoWorkflow exercises every layer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ResultMessage

from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import (
    ActivityEnd,
    ActivityStart,
    AgentEnd,
    AgentStart,
    Event,
)
from src.workflows._smoke.echo import EchoWorkflow


async def test_echo_workflow_runs_end_to_end(tmp_path: Path) -> None:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    ctx = RunContext(
        cwd=tmp_path,
        env={"ECHO_MESSAGE": "hi"},
        workspace=tmp_path,
        events=bus,
    )

    def fake_query(*, prompt: str, options: Any) -> Any:
        async def gen() -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.0,
                usage={"input_tokens": 0, "output_tokens": 0},
                result="hi",
            )

        return gen()

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await EchoWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data == {"text": "hi"}

    kinds = [type(e) for e in received]
    assert AgentStart in kinds
    assert AgentEnd in kinds
    assert ActivityStart in kinds
    assert ActivityEnd in kinds

    activity_names = [e.name for e in received if isinstance(e, ActivityStart)]
    assert "record_echo" in activity_names


async def test_echo_workflow_prefers_issues_over_env(tmp_path: Path) -> None:
    """CLI dispatch has no adaptor for the bare "command:echo" trigger, so
    the message arrives via ctx.issues (see src/runner/run.py building
    RunContext.issues from CLIArgs.urls) rather than ECHO_MESSAGE — the env
    var is a fallback for direct RunContext callers only (see the test above).
    """
    bus = EventBus()
    ctx = RunContext(
        cwd=tmp_path,
        env={"ECHO_MESSAGE": "should not be used"},
        workspace=tmp_path,
        issues=["from cli"],
        events=bus,
    )
    seen_prompts: list[str] = []

    def fake_query(*, prompt: str, options: Any) -> Any:
        seen_prompts.append(prompt)

        async def gen() -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.0,
                usage={"input_tokens": 0, "output_tokens": 0},
                result="echoed",
            )

        return gen()

    with patch("src.agents.base.query", side_effect=fake_query):
        await EchoWorkflow().run(ctx)

    # The real assertion: the agent's prompt carried the ctx.issues message,
    # not the ECHO_MESSAGE env fallback.
    assert any("from cli" in p for p in seen_prompts)
    assert not any("should not be used" in p for p in seen_prompts)
