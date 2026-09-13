"""Display protocol — how a source's pipeline steps appear in the CLI.

Each source adaptor provides a Display implementation that maps
agent launches, script calls, and tool results to Rich UI updates.
The runner is fully generic — it delegates all display decisions here.
"""

from __future__ import annotations

from typing import Any, Protocol

from src.output import ProgressTracker


class Display(Protocol):
    """Interface for source-specific CLI display logic."""

    def on_agent_start(self, tracker: ProgressTracker, agent_info: str) -> None:
        """Map an agent launch to a step label."""
        ...

    def on_tool_call(
        self,
        tracker: ProgressTracker,
        tool_name: str,
        summary: str,
        input_dict: dict[str, Any],
        parent_tool_use_id: str | None,
        tool_use_id: str,
    ) -> None:
        """Map a tool call to a step label and record in trail.

        ``parent_tool_use_id`` is None for orchestrator-level calls and set
        when a subagent emitted the tool call. ``tool_use_id`` is the block
        id, used to register subagent labels (e.g. ``Reviewer[0]``) so the
        trail can prefix the subagent's later tool calls.
        """
        ...

    def on_tool_result(
        self,
        tracker: ProgressTracker,
        tool_name: str,
        output: str,
        input_dict: dict[str, Any],
        parent_tool_use_id: str | None,
    ) -> None:
        """Render result panels from tool output. See ``on_tool_call`` for
        the meaning of ``parent_tool_use_id``."""
        ...
