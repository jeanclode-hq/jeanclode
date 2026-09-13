"""Tests for the sticky status comment body builder.

``build_status_body`` is pure — no DB, no HTTP — so these tests exercise
the completed/running/errors bucketing directly against ``Execution``
instances without needing app/db fixtures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.plugins.container.status_comment import MARKER, build_status_body


def _execution(
    workflow: ExecutionWorkflow,
    status: ExecutionStatus,
    error_detail: str | None = None,
    *,
    retry_at: datetime | None = None,
) -> Execution:
    return Execution(
        id=uuid.uuid4(),
        provider="github",
        workflow=workflow.value,
        status=status.value,
        error_detail=error_detail,
        retry_at=retry_at,
    )


def test_build_status_body_empty_returns_none():
    assert build_status_body({}) is None


def test_build_status_body_includes_marker_and_header():
    latest = {
        ExecutionWorkflow.REVIEW.value: _execution(
            ExecutionWorkflow.REVIEW, ExecutionStatus.COMPLETED
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert body.startswith(MARKER)
    assert "#### Jeanclode Status Update" in body


def test_build_status_body_completed_workflows_listed():
    latest = {
        ExecutionWorkflow.REVIEW.value: _execution(
            ExecutionWorkflow.REVIEW, ExecutionStatus.COMPLETED
        ),
        ExecutionWorkflow.SUMMARY.value: _execution(
            ExecutionWorkflow.SUMMARY, ExecutionStatus.COMPLETED
        ),
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Completed**: Review & Summary" in body
    assert "Errors" not in body
    assert "Running" not in body


def test_build_status_body_zero_completed_shown_as_0():
    latest = {
        ExecutionWorkflow.RESPOND.value: _execution(
            ExecutionWorkflow.RESPOND, ExecutionStatus.RUNNING
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Completed**: 0" in body
    assert "**Running**: Respond" in body


def test_build_status_body_queued_counts_as_running():
    latest = {
        ExecutionWorkflow.FIX.value: _execution(ExecutionWorkflow.FIX, ExecutionStatus.QUEUED)
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Running**: Fix" in body


def test_build_status_body_error_with_detail():
    latest = {
        ExecutionWorkflow.ISSUE_RESOLVE.value: _execution(
            ExecutionWorkflow.ISSUE_RESOLVE,
            ExecutionStatus.FAILED,
            error_detail="container crashed",
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Errors**: Resolve" in body
    assert "> **Resolve**: container crashed" in body


def test_build_status_body_error_without_detail_uses_fallback():
    latest = {
        ExecutionWorkflow.FIX.value: _execution(
            ExecutionWorkflow.FIX, ExecutionStatus.FAILED, error_detail=None
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert "Please check your dashboard for details." in body


def test_build_status_body_error_detail_truncated_to_200_chars():
    long_detail = "x" * 500
    latest = {
        ExecutionWorkflow.FIX.value: _execution(
            ExecutionWorkflow.FIX, ExecutionStatus.FAILED, error_detail=long_detail
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert "x" * 200 in body
    assert "x" * 201 not in body


def test_build_status_body_scheduled_shows_retry_at():
    """ADR-010: a SCHEDULED execution gets its own bucket, rendered live
    from retry_at rather than a stored string."""
    retry_at = datetime(2026, 8, 27, 23, 10, tzinfo=UTC)
    latest = {
        ExecutionWorkflow.RESPOND.value: _execution(
            ExecutionWorkflow.RESPOND, ExecutionStatus.SCHEDULED, retry_at=retry_at
        )
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Deferred**: Respond" in body
    assert "2026-08-27 23:10 UTC" in body
    assert "Errors" not in body
    assert "Running" not in body


def test_build_status_body_scheduled_without_retry_at_shows_soon():
    latest = {
        ExecutionWorkflow.FIX.value: _execution(ExecutionWorkflow.FIX, ExecutionStatus.SCHEDULED)
    }
    body = build_status_body(latest)
    assert body is not None
    assert "retrying at `soon`" in body


def test_build_status_body_mixed_buckets():
    latest = {
        ExecutionWorkflow.REVIEW.value: _execution(
            ExecutionWorkflow.REVIEW, ExecutionStatus.COMPLETED
        ),
        ExecutionWorkflow.SUMMARY.value: _execution(
            ExecutionWorkflow.SUMMARY, ExecutionStatus.FAILED, "boom"
        ),
        ExecutionWorkflow.RESPOND.value: _execution(
            ExecutionWorkflow.RESPOND, ExecutionStatus.RUNNING
        ),
    }
    body = build_status_body(latest)
    assert body is not None
    assert "**Completed**: Review" in body
    assert "**Errors**: Summary" in body
    assert "> **Summary**: boom" in body
    assert "**Running**: Respond" in body
