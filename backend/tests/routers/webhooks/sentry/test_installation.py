"""Tests for Sentry installation webhook consumer."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from api.database import (
    db_create_org,
    db_create_workspace,
    db_get_org_by_external_id,
    db_get_org_by_installation_id,
)
from api.routers.webhooks.sentry.installation import consume_sentry_installation

# -- Helpers ------------------------------------------------------------------


def _make_installation_payload(
    action: str = "created",
    org_slug: str = "my-sentry-org",
    installation_uuid: str = "install-abc-123",
) -> dict:
    """Build a Sentry installation webhook payload."""
    return {
        "action": action,
        "data": {
            "installation": {
                "uuid": installation_uuid,
                "organization": {
                    "slug": org_slug,
                },
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

    with patch("api.routers.webhooks.sentry.installation.get_current_app", return_value=app):
        yield app


# -- Created tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_created_creates_unlinked_org(db_session):
    """installation.created creates an Organization with no workspace."""
    raw = _make_installation_payload()

    with _mock_app(db_session):
        await consume_sentry_installation(raw)

    org = db_get_org_by_external_id(db_session, "my-sentry-org", provider="sentry")
    assert org is not None
    assert org.installation_id == "install-abc-123"
    assert org.workspace_id is None


@pytest.mark.asyncio
async def test_installation_created_idempotent(db_session):
    """Replaying installation.created is idempotent — does not duplicate."""
    raw = _make_installation_payload()

    with _mock_app(db_session):
        await consume_sentry_installation(raw)
        await consume_sentry_installation(raw)

    # Only one Organization should exist
    org = db_get_org_by_installation_id(db_session, "install-abc-123")
    assert org is not None


# -- Deleted tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_deleted(db_session):
    """installation.deleted removes the Organization."""
    workspace = db_create_workspace(db_session, name="del-org", slug="del-org")
    db_create_org(
        db_session,
        workspace_id=workspace.id,
        name="del-org",
        external_org_id="del-org",
        provider="sentry",
        installation_id="install-to-delete",
    )

    raw = _make_installation_payload(
        action="deleted",
        org_slug="del-org",
        installation_uuid="install-to-delete",
    )

    with _mock_app(db_session):
        await consume_sentry_installation(raw)

    assert db_get_org_by_installation_id(db_session, "install-to-delete") is None


@pytest.mark.asyncio
async def test_installation_deleted_not_found(db_session):
    """installation.deleted for unknown installation does not crash."""
    raw = _make_installation_payload(
        action="deleted",
        installation_uuid="nonexistent-install",
    )

    with _mock_app(db_session):
        await consume_sentry_installation(raw)


# -- Edge cases ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_org_slug_discarded(db_session):
    """Consumer discards webhooks with missing organization slug."""
    payload = {
        "action": "created",
        "data": {"installation": {"uuid": "abc", "organization": {}}},
    }

    with _mock_app(db_session) as mock_app:
        await consume_sentry_installation(payload)
        mock_app.database.session.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_action_ignored(db_session):
    """Consumer ignores actions other than created/deleted."""
    payload = _make_installation_payload(action="updated")

    with _mock_app(db_session) as mock_app:
        await consume_sentry_installation(payload)
        mock_app.database.session.assert_not_called()
