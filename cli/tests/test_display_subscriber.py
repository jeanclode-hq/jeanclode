"""TTY display subscriber — merging concurrent agents + tool-call prefixes."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.runner.display_subscriber import UsageAccumulator, subscribe_display
from src.runtime.bus import EventBus
from src.runtime.events import AgentEnd, AgentStart, ToolCall


def _wire() -> tuple[EventBus, MagicMock]:
    bus = EventBus()
    tracker = MagicMock()
    subscribe_display(bus, tracker)
    return bus, tracker


def test_first_agent_calls_step() -> None:
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Analyzer[0]"))
    tracker.step.assert_called_once_with("Running Analyzer[0]...")
    tracker.set_step_label.assert_not_called()


def test_second_concurrent_agent_widens_label_without_finalizing() -> None:
    """A new AgentStart while another is in flight must not finalize the first.

    Before the fix, every AgentStart called ``tracker.step(...)`` which
    finalized the prior step — so Analyzer[0] was shown as completed
    (0.0s) the moment Analyzer[1] started, even with [0] still running.
    """
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Analyzer[0]"))
    bus.publish(AgentStart(name="Analyzer[1]"))
    assert tracker.step.call_count == 1
    tracker.set_step_label.assert_called_once_with("Running Analyzer[0] + Analyzer[1]...")


def test_agent_end_shrinks_label() -> None:
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Analyzer[0]"))
    bus.publish(AgentStart(name="Analyzer[1]"))
    bus.publish(AgentEnd(name="Analyzer[0]"))
    # last label update reflects only the surviving agent
    tracker.set_step_label.assert_called_with("Running Analyzer[1]...")


def test_tool_call_gets_agent_prefix_when_parallel() -> None:
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Analyzer[0]"))
    bus.publish(AgentStart(name="Analyzer[1]"))
    bus.publish(
        ToolCall(agent="Analyzer[1]", name="Grep", summary="pat in path"),
    )
    tracker.on_tool_call.assert_called_with("Grep", "pat in path", prefix="Analyzer[1] -")


def test_tool_call_has_no_prefix_when_single_agent() -> None:
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Synthesizer"))
    bus.publish(ToolCall(agent="Synthesizer", name="Read", summary="foo.py"))
    tracker.on_tool_call.assert_called_with("Read", "foo.py", prefix="")


def test_subagent_tool_call_is_dropped() -> None:
    """Tool calls nested under a parent (Task subagents) stay off the trail."""
    bus, tracker = _wire()
    bus.publish(AgentStart(name="Analyzer[0]"))
    bus.publish(
        ToolCall(
            agent="Analyzer[0]",
            name="Grep",
            summary="x",
            parent_tool_use_id="abc",
        )
    )
    tracker.on_tool_call.assert_not_called()


def test_usage_accumulator_sums_across_agents() -> None:
    bus = EventBus()
    acc = UsageAccumulator()
    bus.subscribe(acc)
    bus.publish(AgentEnd(name="Triage", usage={"input_tokens": 100, "output_tokens": 20}))
    bus.publish(
        AgentEnd(
            name="Fixer", usage={"input_tokens": 300, "output_tokens": 40, "cache_read_tokens": 5}
        )
    )
    assert acc.total.input_tokens == 400
    assert acc.total.output_tokens == 60
    assert acc.total.cache_read_tokens == 5


def test_usage_accumulator_ignores_empty_usage() -> None:
    bus = EventBus()
    acc = UsageAccumulator()
    bus.subscribe(acc)
    bus.publish(AgentEnd(name="Triage"))
    assert acc.total.input_tokens == 0
