"""Shared scheduled-execution retry helpers (ADR-010).

The redispatch poller (:mod:`api.plugins.container.scheduled_dispatch`) only
ever pushes ``{"execution_id": ...}`` onto the originating stream — it never
reconstructs the full dispatch payload itself. Each GitHub/GitLab consumer
that can receive such a message resolves the "already-resolved target" back
out of the ``Execution`` row (and, for RESPOND, its ``retry_target_url``)
before falling through to the same launch call a fresh webhook uses.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from api.database.execution import db_update_execution_status
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue
from api.models.pull_requests import PRState, PullRequest

logger = logging.getLogger(__name__)


class RetryContext(BaseModel):
    """Reconstructed dispatch context for a redispatched SCHEDULED execution."""

    workflow: str
    organization_id: str | None
    pull_request_id: str | None
    issue_id: str | None
    target_url: str | None


def resolve_retry_context(db: Session, execution_id: UUID) -> RetryContext | None:
    """Load ``execution_id`` and reconstruct the dispatch context it needs.

    Returns ``None`` if the execution or its target no longer resolves at
    all (already deleted) — callers should treat that the same as any other
    "not found" case in the fresh-dispatch path.
    """
    execution = (
        db.query(Execution)
        .options(
            selectinload(Execution.pull_requests).joinedload(PullRequest.repository),
            selectinload(Execution.issues).joinedload(Issue.repository),
        )
        .filter(Execution.id == execution_id)
        .first()
    )
    if not execution:
        return None

    if execution.pull_requests:
        pr = execution.pull_requests[0]
        org_id = pr.repository.org_id if pr.repository else None
        return RetryContext(
            workflow=execution.workflow,
            organization_id=str(org_id) if org_id else None,
            pull_request_id=str(pr.id),
            issue_id=None,
            target_url=execution.retry_target_url,
        )

    if execution.issues:
        issue = execution.issues[0]
        org_id = issue.repository.org_id if issue.repository else None
        return RetryContext(
            workflow=execution.workflow,
            organization_id=str(org_id) if org_id else None,
            pull_request_id=None,
            issue_id=str(issue.id),
            target_url=execution.retry_target_url,
        )

    return None


def recheck_pull_request_still_actionable(
    db: Session,
    *,
    execution_id: UUID,
    pull_request_id: UUID,
) -> bool:
    """Re-verify a PR-targeting retry is still worth dispatching.

    A retry can be scheduled hours before it actually redispatches — the PR
    may have closed or merged in the meantime. Marks the execution
    CANCELLED and returns False when that's the case, mirroring "no new
    status is needed" per ADR-010 (CANCELLED is the existing terminal
    status closest to "decided not to run").
    """
    pr = db.query(PullRequest).filter(PullRequest.id == pull_request_id).first()
    if not pr or pr.state != PRState.OPEN.value:
        logger.info(
            "Retry for execution %s skipped — PR %s is no longer open (state=%s)",
            execution_id,
            pull_request_id,
            pr.state if pr else "deleted",
        )
        db_update_execution_status(
            db,
            execution_id,
            ExecutionStatus.CANCELLED.value,
            error_type="stale_on_retry",
            error_detail="Pull request closed or merged before the retry ran",
        )
        return False
    return True


def recheck_issue_still_actionable(
    db: Session,
    *,
    execution_id: UUID,
    issue_id: UUID,
) -> bool:
    """Re-verify an issue-targeting retry is still worth dispatching."""
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue or issue.status == "closed":
        logger.info(
            "Retry for execution %s skipped — issue %s is no longer open (status=%s)",
            execution_id,
            issue_id,
            issue.status if issue else "deleted",
        )
        db_update_execution_status(
            db,
            execution_id,
            ExecutionStatus.CANCELLED.value,
            error_type="stale_on_retry",
            error_detail="Issue closed before the retry ran",
        )
        return False
    return True
