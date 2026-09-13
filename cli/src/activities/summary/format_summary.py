"""Render a refined summary into the final PR/MR comment body."""

from __future__ import annotations

from src.activities.decorator import activity
from src.activities.summary.schemas import ParsedSummary, SummaryPayload
from src.runtime.context import RunContext


@activity(name="Formatting summary")
def format_summary(parsed: ParsedSummary, *, ctx: RunContext) -> SummaryPayload:  # noqa: ARG001
    body = "#### Description\n\n" + parsed.description.strip() + "\n"
    return SummaryPayload(body=body)
