"""Database operations for Issue model."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from api.models.issues import Issue, TriageResult


def db_get_issue_by_sentry_id(
    db: Session,
    repository_id: UUID,
    external_id: str,
) -> Issue | None:
    """Get issue by repository ID and external issue ID."""
    return (
        db.query(Issue)
        .filter(
            Issue.repository_id == repository_id,
            Issue.external_id == external_id,
        )
        .first()
    )


def db_create_issue(
    db: Session,
    repository_id: UUID,
    external_id: str,
    title: str,
    level: str,
    culprit: str | None = None,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
    event_count: int = 0,
    status: str = "unresolved",
    author: str | None = None,
    triage_result: str = TriageResult.PENDING.value,
    issue_url: str | None = None,
) -> Issue:
    """Create a new issue."""
    issue = Issue(
        repository_id=repository_id,
        external_id=external_id,
        title=title,
        culprit=culprit,
        level=level,
        first_seen=first_seen,
        last_seen=last_seen,
        event_count=event_count,
        status=status,
        author=author,
        issue_url=issue_url,
        triage_result=triage_result,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def db_update_issue(
    db: Session,
    issue_id: UUID,
    **fields: Any,
) -> Issue | None:
    """Update an issue's fields."""
    issue = db_get_issue_by_id(db, issue_id)
    if not issue:
        return None

    for key, value in fields.items():
        if hasattr(issue, key):
            setattr(issue, key, value)

    db.commit()
    db.refresh(issue)
    return issue


def db_get_issue_by_id(
    db: Session,
    issue_id: UUID,
) -> Issue | None:
    """Get issue by ID."""
    return db.query(Issue).filter(Issue.id == issue_id).first()
