"""Sentry display — minimal pass-through.

Step labels now come from agent/activity events emitted by the
SentryFixWorkflow itself; the adaptor display layer is kept only to
satisfy the Adaptor protocol while we still expose ``adaptor.display``
for legacy callers.
"""

from __future__ import annotations

from typing import Any

from src.output import ProgressTracker


class SentryDisplay:
    def on_agent_start(self, tracker: ProgressTracker, agent_info: str) -> None:
        if agent_info:
            tracker.step(agent_info)

    def on_tool_call(
        self,
        tracker: ProgressTracker,
        tool_name: str,
        summary: str,
        input_dict: dict[str, Any],  # noqa: ARG002
        parent_tool_use_id: str | None,
        tool_use_id: str,  # noqa: ARG002
    ) -> None:
        if parent_tool_use_id is None:
            tracker.on_tool_call(tool_name, summary)

    def on_tool_result(
        self,
        tracker: ProgressTracker,  # noqa: ARG002
        tool_name: str,  # noqa: ARG002
        output: str,  # noqa: ARG002
        input_dict: dict[str, Any],  # noqa: ARG002
        parent_tool_use_id: str | None,  # noqa: ARG002
    ) -> None:
        return
