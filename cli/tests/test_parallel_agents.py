"""Parallel BaseAgent.invoke under asyncio.gather."""

from __future__ import annotations

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
from src.runtime.events import AgentEnd, Event


class _Input(BaseModel):
    message: str


class _A(BaseAgent):
    name: ClassVar[str] = "A"
    prompt_file: ClassVar[str] = ""
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 1


async def test_parallel_invocations_complete_independently(tmp_path: Path) -> None:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=bus)

    counter = {"n": 0}

    def fake_query(*, prompt: str, options: Any) -> Any:
        counter["n"] += 1
        my_id = counter["n"]

        async def gen() -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=f"s{my_id}",
                total_cost_usd=0.0,
                usage={"input_tokens": 0, "output_tokens": 0},
                result=f"r{my_id}",
            )

        return gen()

    with patch("src.agents.base.query", side_effect=fake_query):
        results = await asyncio.gather(
            _A().invoke(_Input(message="x"), ctx),
            _A().invoke(_Input(message="y"), ctx),
            return_exceptions=True,
        )

    assert {r.text for r in results if hasattr(r, "text")} == {"r1", "r2"}
    end_events = [e for e in received if isinstance(e, AgentEnd)]
    assert len(end_events) == 2
    assert all(e.ok for e in end_events)
