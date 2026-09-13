"""Issue model."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base

if TYPE_CHECKING:
    from api.models.executions import Execution
    from api.models.pull_requests import PullRequest
    from api.models.repositories import Repository


class TriageResult(StrEnum):
    """Triage outcome for an issue."""

    PENDING = "pending"
    ACTIONABLE = "actionable"
    NOT_ACTIONABLE = "not_actionable"


class Issue(Base):
    """Issue from any source (Sentry, Linear, GitHub, etc.)."""

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("repository_id", "external_id", name="uq_issue_repository_external"),
        Index("ix_issues_triage_result", "triage_result"),
        Index("ix_issues_repository_id", "repository_id"),
        Index("ix_issues_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    culprit: Mapped[str | None] = mapped_column(String(500), nullable=True)
    level: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="unresolved")
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    issue_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    triage_result: Mapped[str] = mapped_column(String(50), default=TriageResult.PENDING.value)
    triage_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[assignment]

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    repository: Mapped[Repository] = relationship(back_populates="issues")
    executions: Mapped[list[Execution]] = relationship(
        secondary="execution_issues",
        back_populates="issues",
    )
    pull_requests: Mapped[list[PullRequest]] = relationship(
        secondary="issue_pull_requests",
        back_populates="issues",
    )
