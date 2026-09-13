"""Tests for Sentry webhook consumer."""

import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from api.database import db_get_issue_by_sentry_id
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.workspaces import Workspace
from api.routers.webhooks.sentry.consumer import (
    ACCEPTED_ACTIONS,
    _parse_datetime,
    consume_sentry_webhook,
)

PUBLISH_PATH = "api.routers.webhooks.sentry.consumer.publish_issue_event"


# -- Helpers ------------------------------------------------------------------


def _make_org(db: Session, installation_id: str = "install-abc") -> Organization:
    workspace = Workspace(name="test-workspace", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db.add(workspace)
    db.flush()
    org = Organization(
        workspace_id=workspace.id,
        name="Test Org",
        external_org_id=f"test-org-{uuid.uuid4().hex[:6]}",
        provider="sentry",
        installation_id=installation_id,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _make_repository(
    db: Session,
    org_id: uuid.UUID,
    external_project_id: str = "12345",
) -> Repository:
    sp = Repository(
        org_id=org_id,
        name="my-project",
        external_id=external_project_id,
        provider="sentry",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


def _make_webhook_payload(
    action: str = "created",
    installation_uuid: str = "install-abc",
    sentry_project_id: str = "12345",
    sentry_issue_id: str = "99999",
    title: str = "TypeError: undefined is not a function",
    culprit: str = "app.views.home",
    level: str = "error",
    first_seen: str = "2026-03-20T10:00:00+00:00",
    last_seen: str = "2026-03-23T12:00:00+00:00",
    count: int = 5,
) -> dict:
    return {
        "action": action,
        "installation": {"uuid": installation_uuid},
        "data": {
            "issue": {
                "id": sentry_issue_id,
                "title": title,
                "culprit": culprit,
                "level": level,
                "project": {"id": sentry_project_id},
                "firstSeen": first_seen,
                "lastSeen": last_seen,
                "count": count,
            }
        },
    }


@contextmanager
def _mock_app(db_session: Session):
    """Mock get_current_app to return an app with a database plugin using the test session."""
    db_plugin = MagicMock()
    db_plugin.session.return_value.__enter__ = MagicMock(return_value=db_session)
    db_plugin.session.return_value.__exit__ = MagicMock(return_value=False)

    app = MagicMock()
    app.database = db_plugin

    with patch("api.routers.webhooks.sentry.consumer.get_current_app", return_value=app):
        yield app


# -- Unit tests for helpers ---------------------------------------------------


def test_parse_datetime_valid():
    dt = _parse_datetime("2026-03-23T12:00:00+00:00")
    assert isinstance(dt, datetime)


def test_parse_datetime_none():
    assert _parse_datetime(None) is None


def test_parse_datetime_invalid():
    assert _parse_datetime("not-a-date") is None


# -- Consumer integration tests -----------------------------------------------


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_creates_issue(mock_publish, db_session):
    """Consumer creates a new issue for a valid 'created' webhook."""
    org = _make_org(db_session)
    sp = _make_repository(db_session, org.id)
    raw = _make_webhook_payload()

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    issue = db_get_issue_by_sentry_id(db_session, sp.id, "99999")
    assert issue is not None
    assert issue.title == "TypeError: undefined is not a function"
    assert issue.culprit == "app.views.home"
    assert issue.level == "error"
    assert issue.event_count == 5
    assert issue.triage_result == TriageResult.PENDING

    mock_publish.assert_awaited_once()
    call_kwargs = mock_publish.call_args.kwargs
    assert call_kwargs["action"] == "created"
    assert call_kwargs["workspace_id"] == str(org.workspace_id)


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_updates_existing_issue(mock_publish, db_session):
    """Consumer updates an existing issue on subsequent webhooks."""
    org = _make_org(db_session)
    sp = _make_repository(db_session, org.id)

    # Create existing issue
    existing = Issue(
        repository_id=sp.id,
        external_id="99999",
        title="Old title",
        level="warning",
        event_count=1,
        triage_result=TriageResult.ACTIONABLE.value,
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    raw = _make_webhook_payload(action="regression", count=10)

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    db_session.refresh(existing)
    assert existing.title == "TypeError: undefined is not a function"
    assert existing.event_count == 10
    assert existing.level == "error"

    call_kwargs = mock_publish.call_args.kwargs
    assert call_kwargs["action"] == "updated"


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_resolved_updates_issue(mock_publish, db_session):
    """Consumer processes 'resolved' action and updates fields."""
    org = _make_org(db_session)
    sp = _make_repository(db_session, org.id)

    existing = Issue(
        repository_id=sp.id,
        external_id="99999",
        title="Old title",
        level="error",
        event_count=3,
        triage_result=TriageResult.ACTIONABLE.value,
    )
    db_session.add(existing)
    db_session.commit()

    raw = _make_webhook_payload(action="resolved", count=3)

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    db_session.refresh(existing)
    assert existing.title == "TypeError: undefined is not a function"
    mock_publish.assert_awaited_once()


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_ignores_unknown_action(mock_publish, db_session):
    """Consumer ignores webhook actions not in ACCEPTED_ACTIONS."""
    raw = _make_webhook_payload(action="assigned")

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    mock_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_discards_unknown_action():
    """Consumer discards payloads with an unrecognized action."""
    with patch("api.routers.webhooks.sentry.consumer.get_current_app") as mock:
        await consume_sentry_webhook({"action": "assigned"})
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_consume_discards_missing_installation(db_session):
    """Consumer discards webhooks without installation.uuid."""
    payload = {"action": "created", "data": {"issue": {}}}

    with _mock_app(db_session) as mock_app:
        # Should not reach DB lookups
        mock_app.database.session.assert_not_called()
        await consume_sentry_webhook(payload)


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_discards_unknown_installation(mock_publish, db_session):
    """Consumer discards webhooks with unrecognized installation UUID."""
    raw = _make_webhook_payload(installation_uuid="unknown-install")

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    mock_publish.assert_not_awaited()


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_discards_unknown_project(mock_publish, db_session):
    """Consumer discards webhooks with unrecognized Sentry project ID."""
    _make_org(db_session)
    # Don't create any projects
    raw = _make_webhook_payload(sentry_project_id="nonexistent")

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    mock_publish.assert_not_awaited()


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_resolved_skips_unknown_issue(mock_publish, db_session):
    """Resolved webhook for an unknown issue does not create a PENDING issue."""
    org = _make_org(db_session)
    sp = _make_repository(db_session, org.id)
    raw = _make_webhook_payload(action="resolved", sentry_issue_id="never-seen")

    with _mock_app(db_session):
        await consume_sentry_webhook(raw)

    issue = db_get_issue_by_sentry_id(db_session, sp.id, "never-seen")
    assert issue is None
    mock_publish.assert_not_awaited()


@pytest.mark.asyncio
@patch(PUBLISH_PATH, new_callable=AsyncMock)
async def test_consume_handles_null_count(mock_publish, db_session):
    """Consumer handles count: null without crashing."""
    org = _make_org(db_session)
    sp = _make_repository(db_session, org.id)

    # Build payload with count: null
    payload = {
        "action": "created",
        "installation": {"uuid": "install-abc"},
        "data": {
            "issue": {
                "id": "88888",
                "title": "NullCount",
                "culprit": None,
                "level": "error",
                "project": {"id": "12345"},
                "firstSeen": None,
                "lastSeen": None,
                "count": None,
            }
        },
    }

    with _mock_app(db_session):
        await consume_sentry_webhook(payload)

    issue = db_get_issue_by_sentry_id(db_session, sp.id, "88888")
    assert issue is not None
    assert issue.event_count == 0


def test_accepted_actions():
    """Verify accepted actions match issue spec."""
    assert {"created", "regression", "resolved", "unresolved"} == ACCEPTED_ACTIONS
