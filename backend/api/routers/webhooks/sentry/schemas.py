"""Sentry webhook schemas for triage logic."""

from enum import StrEnum

from pydantic import BaseModel

from api.models.issues import TriageResult


class Decision(StrEnum):
    """Outcome of the issue processing decision tree."""

    ACCEPT = "accept"
    SKIP = "skip"
    RETRY = "retry"


class IssueRecord(BaseModel):
    """Snapshot of a known issue's state for decision-making.

    Populated from the Issue DB model + Execution queries by the caller.
    """

    triage_result: TriageResult | None = None
    has_active_execution: bool = False
    failed_execution_count: int = 0
    has_completed_execution: bool = False
    # TODO: add fix_release when regression detection is implemented


class SentryEvent(BaseModel):
    """Relevant fields from an incoming Sentry webhook event."""

    release: str | None = None
