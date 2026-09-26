"""memory-curate: batches due entries through the curator and marks each batch curated."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ResultMessage

from src.agents.memory_tool import MEMORY_TOOL_NAMES
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.workflows.memory_curate import runner
from src.workflows.memory_curate.runner import MemoryCurateWorkflow


def _ctx(tmp_path: Path, *, memory_enabled: bool = True) -> RunContext:
    return RunContext(
        cwd=tmp_path, workspace=tmp_path, events=EventBus(), memory_enabled=memory_enabled
    )


def _fake_query(prompts: list[str], options_seen: list[Any]) -> Any:
    def fake_query(*, prompt: str, options: Any) -> Any:
        prompts.append(prompt)
        options_seen.append(options)

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
                result="kept 1, deleted 1",
            )

        return gen()

    return fake_query


def _fake_backend(due: list[str], marked: list[list[str]]) -> Any:
    async def backend_request(
        method: str, path: str, *, params: Any = None, json_body: Any = None
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/internal/memory/curation":
            return 200, {"paths": due}
        if method == "POST" and path == "/internal/memory/curation/mark":
            marked.append(json_body["paths"])
            return 200, {"marked_count": len(json_body["paths"])}
        raise AssertionError(f"unexpected call {method} {path}")

    return backend_request


async def test_curates_due_entries_in_batches_and_marks_each(tmp_path: Path) -> None:
    due = [f"repo/note-{i:02d}.md" for i in range(runner.BATCH_SIZE + 3)]
    prompts: list[str] = []
    options_seen: list[Any] = []
    marked: list[list[str]] = []

    with (
        patch("src.activities.memory.curation.backend_request", _fake_backend(due, marked)),
        patch("src.agents.base.query", _fake_query(prompts, options_seen)),
    ):
        result = await MemoryCurateWorkflow().run(_ctx(tmp_path))

    assert result.status == "success"
    assert marked == [due[: runner.BATCH_SIZE], due[runner.BATCH_SIZE :]]
    assert "`repo/note-00.md`" in prompts[0]
    assert "`repo/note-00.md`" not in prompts[1]
    assert result.data["marked"] == len(due)


async def test_curator_session_gets_memory_tools_and_no_builtin_tools(tmp_path: Path) -> None:
    prompts: list[str] = []
    options_seen: list[Any] = []

    with (
        patch("src.activities.memory.curation.backend_request", _fake_backend(["a/b.md"], [])),
        patch("src.agents.base.query", _fake_query(prompts, options_seen)),
    ):
        await MemoryCurateWorkflow().run(_ctx(tmp_path))

    options = options_seen[0]
    assert options.tools == []
    assert set(options.allowed_tools) == set(MEMORY_TOOL_NAMES)
    assert "=== Memory ===" in prompts[0]


async def test_curates_every_due_entry_in_one_run(tmp_path: Path) -> None:
    due = [f"r/n{i}.md" for i in range(runner.BATCH_SIZE * 9 + 3)]
    marked: list[list[str]] = []

    with (
        patch("src.activities.memory.curation.backend_request", _fake_backend(due, marked)),
        patch("src.agents.base.query", _fake_query([], [])),
    ):
        result = await MemoryCurateWorkflow().run(_ctx(tmp_path))

    assert len(marked) == 10
    assert sorted(p for batch in marked for p in batch) == sorted(due)
    assert result.data["due"] == len(due)


async def test_nothing_due_runs_no_agent(tmp_path: Path) -> None:
    prompts: list[str] = []

    with (
        patch("src.activities.memory.curation.backend_request", _fake_backend([], [])),
        patch("src.agents.base.query", _fake_query(prompts, [])),
    ):
        result = await MemoryCurateWorkflow().run(_ctx(tmp_path))

    assert result.status == "success"
    assert prompts == []


async def test_errors_without_memory_configured(tmp_path: Path) -> None:
    result = await MemoryCurateWorkflow().run(_ctx(tmp_path, memory_enabled=False))
    assert result.status == "error"
