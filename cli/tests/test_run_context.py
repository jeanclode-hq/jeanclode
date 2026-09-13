"""RunContext derivation and emission."""

from __future__ import annotations

from pathlib import Path

from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import AgentStart, Event


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        cwd=tmp_path,
        env={"FOO": "bar"},
        workspace=tmp_path,
        events=EventBus(),
    )


def test_with_cwd_returns_new_context_with_shared_bus(tmp_path: Path) -> None:
    base = _ctx(tmp_path)
    other = tmp_path / "sub"
    other.mkdir()
    derived = base.with_cwd(other)

    assert derived.cwd == other
    assert base.cwd == tmp_path
    assert derived.events is base.events  # shared by reference
    assert derived.env == base.env


def test_emit_round_trips_through_bus(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    received: list[Event] = []
    ctx.events.subscribe(received.append)

    ctx.emit(AgentStart(name="hello"))

    assert len(received) == 1
    assert received[0].kind == "agent.start"


def test_with_cwd_emissions_reach_original_subscribers(tmp_path: Path) -> None:
    base = _ctx(tmp_path)
    received: list[Event] = []
    base.events.subscribe(received.append)

    other = tmp_path / "sub"
    other.mkdir()
    derived = base.with_cwd(other)
    derived.emit(AgentStart(name="from-derived"))

    assert len(received) == 1
