"""Typed events emitted on the run-scoped event bus.

Workflow authors never construct these directly — `BaseAgent.invoke` and
`@activity` emit them automatically. Subscribers (Rich UI, JSON logger,
tests) receive a typed `Event` discriminated by its `kind` literal.
"""

from typing import Literal

from pydantic import BaseModel, Field


class AgentStart(BaseModel):
    kind: Literal["agent.start"] = "agent.start"
    name: str


class AgentEnd(BaseModel):
    kind: Literal["agent.end"] = "agent.end"
    name: str
    ok: bool = True
    duration_ms: int = 0
    usage: dict[str, int] = Field(default_factory=dict)


class ActivityStart(BaseModel):
    kind: Literal["activity.start"] = "activity.start"
    name: str


class ActivityEnd(BaseModel):
    kind: Literal["activity.end"] = "activity.end"
    name: str
    ok: bool = True
    duration_ms: int = 0


class ToolCall(BaseModel):
    kind: Literal["tool.call"] = "tool.call"
    agent: str
    name: str
    summary: str = ""
    input: dict[str, object] = Field(default_factory=dict)
    parent_tool_use_id: str | None = None
    tool_use_id: str = ""


class ToolResult(BaseModel):
    kind: Literal["tool.result"] = "tool.result"
    agent: str
    name: str
    output: str = ""
    parent_tool_use_id: str | None = None


class Panel(BaseModel):
    """A workflow-emitted block of text the display layer renders as a panel."""

    kind: Literal["panel"] = "panel"
    title: str
    content: str
    style: str = "cyan"


type Event = AgentStart | AgentEnd | ActivityStart | ActivityEnd | ToolCall | ToolResult | Panel
