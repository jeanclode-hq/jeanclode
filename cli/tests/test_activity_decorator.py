"""@activity emission semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.activities.decorator import activity
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import ActivityEnd, ActivityStart, Event


def _ctx(tmp_path: Path) -> tuple[RunContext, list[Event]]:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    return (
        RunContext(cwd=tmp_path, workspace=tmp_path, events=bus),
        received,
    )


def test_sync_activity_emits_start_and_end_on_success(tmp_path: Path) -> None:
    @activity
    def add(x: int, y: int, *, ctx: RunContext) -> int:
        return x + y

    ctx, received = _ctx(tmp_path)
    assert add(1, 2, ctx=ctx) == 3

    assert [type(e) for e in received] == [ActivityStart, ActivityEnd]
    end = received[1]
    assert isinstance(end, ActivityEnd)
    assert end.ok is True
    assert end.name == "add"


async def test_async_activity_emits_start_and_end_on_success(tmp_path: Path) -> None:
    @activity
    async def add_async(x: int, *, ctx: RunContext) -> int:
        return x + 1

    ctx, received = _ctx(tmp_path)
    assert await add_async(4, ctx=ctx) == 5
    assert [type(e) for e in received] == [ActivityStart, ActivityEnd]


def test_activity_emits_failed_end_and_reraises(tmp_path: Path) -> None:
    @activity
    def boom(*, ctx: RunContext) -> None:
        raise RuntimeError("nope")

    ctx, received = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="nope"):
        boom(ctx=ctx)

    assert isinstance(received[-1], ActivityEnd)
    assert received[-1].ok is False


def test_activity_uses_display_name_when_provided(tmp_path: Path) -> None:
    @activity(name="Doing the thing")
    def thinger(*, ctx: RunContext) -> int:
        return 7

    ctx, received = _ctx(tmp_path)
    assert thinger(ctx=ctx) == 7
    assert [e.name for e in received] == ["Doing the thing", "Doing the thing"]


def test_activity_requires_ctx_kwarg(tmp_path: Path) -> None:
    @activity
    def noop(*, ctx: RunContext) -> None:
        return None

    with pytest.raises(TypeError, match="ctx: RunContext"):
        noop()  # type: ignore[call-arg]

    # Also reject non-RunContext values
    with pytest.raises(TypeError, match="ctx: RunContext"):
        noop(ctx="not a context")  # type: ignore[arg-type]

    _ = tmp_path  # silence ARG
