"""Execution model — tracks agent workflow runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base

if TYPE_CHECKING:
    from api.models.issues import Issue
    from api.models.pull_requests import PullRequest


class ExecutionStatus(StrEnum):
    """Status of an agent execution."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Admitted but every LLM credential is currently exhausted (ADR-010).
    # Distinct from QUEUED, which retains its existing meaning. Redispatched
    # by a periodic task once `retry_at` elapses; cancellable via a single
    # conditional status update since no container has been started yet.
    SCHEDULED = "scheduled"


class ExecutionTrigger(StrEnum):
    """How an execution was triggered."""

    AUTO = "auto"
    MANUAL = "manual"


class ExecutionWorkflow(StrEnum):
    """Type of workflow an execution performs."""

    FIX = "fix"
    REVIEW = "review"
    SUMMARY = "summary"
    RESPOND = "respond"
    ISSUE_RESOLVE = "issue_resolve"


# Workflows whose state is an issue's status. A RESPOND run also links the issue
# it was mentioned on, but it's a side conversation, not progress on the fix.
ISSUE_STATUS_WORKFLOWS = (ExecutionWorkflow.FIX.value, ExecutionWorkflow.ISSUE_RESOLVE.value)


class Execution(Base):
    """A single agent workflow run.

    A FIX execution links to one or more :class:`Issue` rows via the
    ``execution_issues`` join table. REVIEW/SUMMARY executions link to one or
    more :class:`PullRequest` rows via ``execution_pull_requests``. The two
    target types never mix in a single execution.
    """

    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_status", "status"),
        Index("ix_executions_created_at", "created_at"),
        Index("ix_executions_provider_status", "provider", "status"),
        Index("ix_executions_status_retry_at", "status", "retry_at"),
        Index("ix_executions_triggered_by_identity_id", "triggered_by_identity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Container plugin that runs this execution (github / gitlab / sentry).
    # Scopes reconciler queries — without this, a github reconciler tick can
    # mark sentry-owned executions stale and vice versa.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    workflow: Mapped[str] = mapped_column(String(50), default=ExecutionWorkflow.FIX.value)
    trigger: Mapped[str] = mapped_column(String(50), default=ExecutionTrigger.AUTO.value)
    status: Mapped[str] = mapped_column(String(50), default=ExecutionStatus.QUEUED.value)
    container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Set only for SCHEDULED executions (ADR-010) — when the redispatch
    # poller should next try to claim this row. Rendered live by the status
    # comment rather than duplicated into a stored string.
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set at creation for RESPOND executions only — the mention comment's
    # URL, which encodes which comment/thread triggered the run. Nothing
    # else on this row (or its issue/PR relations) can reconstruct that if
    # the execution is later retried via the scheduled-redispatch path.
    retry_target_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # The @jeanclode-bot mention/comment body that triggered a RESPOND
    # execution, surfaced read-only in the dashboard detail modal.
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who @-mentioned the bot (RESPOND), when their provider identity is synced.
    triggered_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_identities.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    issues: Mapped[list[Issue]] = relationship(
        secondary="execution_issues",
        back_populates="executions",
        lazy="selectin",
    )
    pull_requests: Mapped[list[PullRequest]] = relationship(
        secondary="execution_pull_requests",
        back_populates="executions",
        lazy="selectin",
    )
