"""EventBus pub/sub semantics."""

from __future__ import annotations

from src.runtime.bus import EventBus
from src.runtime.events import ActivityStart, AgentStart, Event


def test_publish_fans_out_to_all_subscribers() -> None:
    bus = EventBus()
    received_a: list[Event] = []
    received_b: list[Event] = []
    bus.subscribe(received_a.append)
    bus.subscribe(received_b.append)

    bus.publish(AgentStart(name="x"))
    bus.publish(ActivityStart(name="y"))

    assert [e.kind for e in received_a] == ["agent.start", "activity.start"]
    assert [e.kind for e in received_b] == ["agent.start", "activity.start"]


def test_unsubscribe_removes_handler() -> None:
    bus = EventBus()
    received: list[Event] = []
    unsub = bus.subscribe(received.append)

    bus.publish(AgentStart(name="a"))
    unsub()
    bus.publish(AgentStart(name="b"))

    assert [e.name for e in received] == ["a"]  # type: ignore[union-attr]


def test_handler_exception_is_swallowed() -> None:
    bus = EventBus()

    def raises(_event: Event) -> None:
        raise RuntimeError("boom")

    received: list[Event] = []
    bus.subscribe(raises)
    bus.subscribe(received.append)

    bus.publish(AgentStart(name="ok"))

    assert len(received) == 1
