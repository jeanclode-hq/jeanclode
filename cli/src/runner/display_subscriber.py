"""Bridge run-scoped events to the display surfaces.

In TTY mode, route events into a `ProgressTracker` directly — workflows
name their own activities/agents, so there's no platform-specific
mapping to do (the legacy adaptor `Display` indirection only existed to
translate generic SDK tool calls into per-skill step labels). In
container mode, emit structured JSON via `output.emit_step` /
`emit_tool` so logs land in the same `[JEANCLODE:...]` channels the
container watcher already understands.
"""

import contextlib

from src.agents.schemas import AgentUsage
from src.output import ProgressTracker, emit_step, emit_tool
from src.runtime.bus import EventBus
from src.runtime.events import (
    ActivityEnd,
    ActivityStart,
    AgentEnd,
    AgentStart,
    Event,
    Panel,
    ToolCall,
    ToolResult,
)


def subscribe_display(events: EventBus, tracker: ProgressTracker | None) -> None:
    if tracker is not None:
        events.subscribe(_TtyHandler(tracker))
    else:
        events.subscribe(_container_handler)


class UsageAccumulator:
    """Sums token usage from every agent invocation over the life of a run.

    Subscribed alongside the display handler so the final run summary can
    report total tokens regardless of how many agents/phases ran or how
    much of the workflow was parallel.
    """

    def __init__(self) -> None:
        self.total = AgentUsage()

    def __call__(self, event: Event) -> None:
        if isinstance(event, AgentEnd) and event.usage:
            self.total = self.total + AgentUsage(**event.usage)


class _TtyHandler:
    """Translate events into ProgressTracker calls.

    Concurrent agents (e.g. the two parallel analyzers) collapse into a
    single step whose spinner label widens as agents join and shrinks as
    they finish. Tool calls from inside those agents get their agent name
    prefixed (``Analyzer[1] - Grep ...``) so the trail stays
    attributable. Subagent tool activity (``parent_tool_use_id`` set)
    is dropped — only orchestrator-level tool calls reach the trail so
    the UI stays readable.
    """

    def __init__(self, tracker: ProgressTracker) -> None:
        self._tracker = tracker
        self._active_agents: list[str] = []

    def __call__(self, event: Event) -> None:
        match event:
            case AgentStart(name=name):
                first = not self._active_agents
                self._active_agents.append(name)
                label = self._agent_label()
                if first:
                    self._tracker.step(label)
                else:
                    self._tracker.set_step_label(label)
            case AgentEnd(name=name):
                with contextlib.suppress(ValueError):
                    self._active_agents.remove(name)
                if self._active_agents:
                    self._tracker.set_step_label(self._agent_label())
                # Empty list: next step() or finish() will flush this one.
            case ActivityStart(name=name):
                self._tracker.step(name)
            case ActivityEnd():
                if event.ok:
                    self._tracker.done("ok")
                else:
                    self._tracker.fail(event.name)
            case ToolCall():
                if event.parent_tool_use_id is None:
                    prefix = f"{event.agent} -" if len(self._active_agents) > 1 else ""
                    self._tracker.on_tool_call(event.name, event.summary, prefix=prefix)
            case ToolResult():
                pass  # tool result is implicit in the next tool_call
            case Panel():
                self._tracker.panel(event.content, event.title, style=event.style)

    def _agent_label(self) -> str:
        """Build a ``Running A + B...`` label from the currently-active agents."""
        return "Running " + " + ".join(self._active_agents) + "..."


def _container_handler(event: Event) -> None:
    match event:
        case AgentStart(name=name):
            emit_step(f"agent:{name}", "started")
        case AgentEnd():
            emit_step(
                f"agent:{event.name}",
                "completed" if event.ok else "failed",
                duration=event.duration_ms / 1000,
            )
        case ActivityStart(name=name):
            emit_step(f"activity:{name}", "started")
        case ActivityEnd():
            emit_step(
                f"activity:{event.name}",
                "completed" if event.ok else "failed",
                duration=event.duration_ms / 1000,
            )
        case ToolCall():
            emit_tool(event.name, event.summary)
        case ToolResult():
            emit_tool(event.name, "", result=event.output)
        case Panel():
            emit_step(f"panel:{event.title}", "info", detail=event.content)
