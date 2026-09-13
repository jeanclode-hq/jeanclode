"""Tests for issue processing triage logic (ADR-001)."""

from api.models.issues import TriageResult
from api.routers.webhooks.sentry.schemas import Decision, IssueRecord, SentryEvent
from api.routers.webhooks.sentry.triage import (
    MAX_RETRIES,
    decide,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _record(
    triage_result: TriageResult | None = TriageResult.ACTIONABLE,
    has_active_execution: bool = False,
    failed_execution_count: int = 0,
    has_completed_execution: bool = False,
) -> IssueRecord:
    return IssueRecord(
        triage_result=triage_result,
        has_active_execution=has_active_execution,
        failed_execution_count=failed_execution_count,
        has_completed_execution=has_completed_execution,
    )


def _event(release: str | None = None) -> SentryEvent:
    return SentryEvent(release=release)


# ---------------------------------------------------------------------------
# Brand-new issue (never seen)
# ---------------------------------------------------------------------------
def test_new_issue_accepted() -> None:
    assert decide(None, _event()) == Decision.ACCEPT


def test_new_issue_accepted_with_release_tag() -> None:
    assert decide(None, _event(release="v1.2.0")) == Decision.ACCEPT


# ---------------------------------------------------------------------------
# Not actionable — always skip
# ---------------------------------------------------------------------------
def test_not_actionable_skipped() -> None:
    record = _record(triage_result=TriageResult.NOT_ACTIONABLE)
    assert decide(record, _event()) == Decision.SKIP


# ---------------------------------------------------------------------------
# Active execution (queued/running) — skip
# ---------------------------------------------------------------------------
def test_active_execution_skipped() -> None:
    record = _record(has_active_execution=True)
    assert decide(record, _event()) == Decision.SKIP


# ---------------------------------------------------------------------------
# Completed execution (PR created) — skip (TODO: regression detection)
# ---------------------------------------------------------------------------
def test_completed_execution_skipped() -> None:
    record = _record(has_completed_execution=True)
    assert decide(record, _event()) == Decision.SKIP


# ---------------------------------------------------------------------------
# Failed — retry with cap
# ---------------------------------------------------------------------------
def test_failed_first_retry() -> None:
    record = _record(failed_execution_count=1)
    assert decide(record, _event()) == Decision.RETRY


def test_failed_second_retry() -> None:
    record = _record(failed_execution_count=2)
    assert decide(record, _event()) == Decision.RETRY


def test_failed_max_retries_exhausted() -> None:
    record = _record(failed_execution_count=MAX_RETRIES)
    assert decide(record, _event()) == Decision.SKIP


def test_failed_beyond_max_retries() -> None:
    record = _record(failed_execution_count=MAX_RETRIES + 1)
    assert decide(record, _event()) == Decision.SKIP


# ---------------------------------------------------------------------------
# Actionable with no executions — already queued for dispatch, skip
# ---------------------------------------------------------------------------
def test_actionable_no_executions_skipped() -> None:
    record = _record(triage_result=TriageResult.ACTIONABLE)
    assert decide(record, _event()) == Decision.SKIP
