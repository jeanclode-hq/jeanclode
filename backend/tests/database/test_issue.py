"""Tests for issue database helpers."""

import uuid

from sqlalchemy.orm import Session

from api.database.issue import (
    db_create_issue,
    db_get_issue_by_id,
    db_get_issue_by_sentry_id,
    db_update_issue,
)
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.workspaces import Workspace


def _create_org(db: Session, **kwargs) -> Organization:
    """Helper to create a test organization."""
    workspace = Workspace(name="test-workspace", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db.add(workspace)
    db.flush()
    defaults = {
        "workspace_id": workspace.id,
        "name": "test-sentry-org",
        "external_org_id": f"test-org-{uuid.uuid4().hex[:6]}",
        "provider": "sentry",
    }
    defaults.update(kwargs)
    org = Organization(**defaults)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _create_repository(db: Session, org_id: uuid.UUID) -> Repository:
    """Helper to create a test source project."""
    sp = Repository(
        org_id=org_id,
        name="test-project",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


def _create_issue(
    db: Session,
    repository_id: uuid.UUID,
    **kwargs,
) -> Issue:
    """Helper to create a test issue."""
    defaults = {
        "repository_id": repository_id,
        "external_id": f"issue-{uuid.uuid4().hex[:8]}",
        "title": "Test error",
        "level": "error",
        "triage_result": TriageResult.ACTIONABLE.value,
    }
    defaults.update(kwargs)
    issue = Issue(**defaults)
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def test_get_issue_by_sentry_id(db_session):
    """Get an issue by source_project_id and external_id."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)
    issue = _create_issue(db_session, sp.id, external_id="SENTRY-123")

    found = db_get_issue_by_sentry_id(db_session, sp.id, "SENTRY-123")
    assert found is not None
    assert found.id == issue.id


def test_get_issue_by_sentry_id_not_found(db_session):
    """Returns None when no matching issue exists."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)

    result = db_get_issue_by_sentry_id(db_session, sp.id, "nonexistent")
    assert result is None


def test_create_issue(db_session):
    """Create a new issue via db_create_issue."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)

    issue = db_create_issue(
        db=db_session,
        repository_id=sp.id,
        external_id="SENTRY-456",
        title="ReferenceError: x is not defined",
        level="error",
        culprit="app.views.index",
        event_count=3,
    )

    assert issue.id is not None
    assert issue.external_id == "SENTRY-456"
    assert issue.title == "ReferenceError: x is not defined"
    assert issue.culprit == "app.views.index"
    assert issue.event_count == 3
    assert issue.triage_result == TriageResult.PENDING


def test_create_issue_not_actionable(db_session):
    """Create an issue with not_actionable triage result."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)

    issue = db_create_issue(
        db=db_session,
        repository_id=sp.id,
        external_id="SENTRY-NA",
        title="Not actionable",
        level="info",
        triage_result=TriageResult.NOT_ACTIONABLE,
    )

    assert issue.triage_result == TriageResult.NOT_ACTIONABLE


def test_create_issue_default_triage(db_session):
    """Create an issue with default triage result (pending)."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)

    issue = db_create_issue(
        db=db_session,
        repository_id=sp.id,
        external_id="SENTRY-NT",
        title="Default triage",
        level="error",
    )

    assert issue.triage_result == TriageResult.PENDING
    assert issue.status == "unresolved"


def test_update_issue(db_session):
    """Update specific fields on an issue."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)
    issue = _create_issue(db_session, sp.id, title="Old title", event_count=1)

    updated = db_update_issue(db_session, issue.id, title="New title", event_count=10)
    assert updated is not None
    assert updated.title == "New title"
    assert updated.event_count == 10


def test_update_issue_triage_result(db_session):
    """Update triage_result on an issue."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)
    issue = _create_issue(db_session, sp.id)

    updated = db_update_issue(db_session, issue.id, triage_result=TriageResult.NOT_ACTIONABLE.value)
    assert updated is not None
    assert updated.triage_result == TriageResult.NOT_ACTIONABLE


def test_update_issue_not_found(db_session):
    """Update returns None for non-existent issue."""
    result = db_update_issue(db_session, uuid.uuid4(), title="nope")
    assert result is None


def test_get_issue_by_id(db_session):
    """Get an issue by its UUID."""
    org = _create_org(db_session)
    sp = _create_repository(db_session, org.id)
    issue = _create_issue(db_session, sp.id)

    found = db_get_issue_by_id(db_session, issue.id)
    assert found is not None
    assert found.id == issue.id
    assert found.title == "Test error"


def test_get_issue_by_id_not_found(db_session):
    """Returns None for non-existent issue."""
    result = db_get_issue_by_id(db_session, uuid.uuid4())
    assert result is None
