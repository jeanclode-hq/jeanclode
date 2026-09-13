"""PullRequest model."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base

if TYPE_CHECKING:
    from api.models.executions import Execution
    from api.models.issues import Issue
    from api.models.repositories import Repository


class PRState(StrEnum):
    """Pull request state."""

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class PullRequest(Base):
    """Pull request or merge request from GitHub/GitLab."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", name="uq_pull_requests_repo_pr"),
        Index("ix_pull_requests_repository_id", "repository_id"),
        Index("ix_pull_requests_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    pr_number: Mapped[int] = mapped_column(Integer)
    external_pr_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(50), default=PRState.OPEN.value)
    pr_url: Mapped[str] = mapped_column(String(500))
    head_branch: Mapped[str] = mapped_column(String(255))
    base_branch: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    repository: Mapped[Repository] = relationship(back_populates="pull_requests")
    executions: Mapped[list[Execution]] = relationship(
        secondary="execution_pull_requests",
        back_populates="pull_requests",
    )
    issues: Mapped[list[Issue]] = relationship(
        secondary="issue_pull_requests",
        back_populates="pull_requests",
    )
