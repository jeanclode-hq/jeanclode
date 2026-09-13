"""Association tables linking Executions to their Issue or PullRequest targets.

A FIX-workflow execution batches issues; REVIEW/SUMMARY-workflow executions batch
pull requests. The two are kept in separate tables so foreign key integrity is
preserved on both sides without resorting to polymorphic columns.

``issue_pull_requests`` is a finer-grained third link: a FIX execution's batch
can be split by synthesis into several PRs, one per root-cause group, so
knowing an issue's *execution* doesn't say which of that execution's PRs
actually addresses it. This table records that directly.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Table, func
from sqlalchemy.dialects.postgresql import UUID

from api.models.base import Base

execution_issues = Table(
    "execution_issues",
    Base.metadata,
    Column(
        "execution_id",
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "issue_id",
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
    Index("ix_execution_issues_issue_id", "issue_id"),
)

execution_pull_requests = Table(
    "execution_pull_requests",
    Base.metadata,
    Column(
        "execution_id",
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "pull_request_id",
        UUID(as_uuid=True),
        ForeignKey("pull_requests.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
    Index("ix_execution_pull_requests_pull_request_id", "pull_request_id"),
)

issue_pull_requests = Table(
    "issue_pull_requests",
    Base.metadata,
    Column(
        "issue_id",
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "pull_request_id",
        UUID(as_uuid=True),
        ForeignKey("pull_requests.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
    Index("ix_issue_pull_requests_pull_request_id", "pull_request_id"),
)
