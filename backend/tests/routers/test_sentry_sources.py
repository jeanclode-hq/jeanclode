"""Tests for Sentry source linking and project sync."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api.database import (
    db_create_org,
    db_create_workspace,
    db_get_org_by_external_id,
    db_get_repositories_by_org,
)
from api.database.workspace import db_create_workspace_membership
from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.organizations import OrgMembership
from api.models.users import User
from api.plugins.sentry.models import SentryOrganization
from api.plugins.sentry.models import SentryProject as SentryProjectAPI
from api.plugins.web.session import SessionData


@pytest.fixture
def sentry_app(app):
    """App fixture with Sentry plugin and tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)
    yield app
    Base.metadata.drop_all(bind=db.engine)


class AuthUser:
    """Container for test user info (avoids detached session issues)."""

    def __init__(self, user_id, identity_id):
        self.id = user_id
        self.identity_id = identity_id


@pytest.fixture
def auth_user(sentry_app):
    """Create an authenticated user with a provider identity."""
    with sentry_app.database.session() as db:
        user = User(email="auth@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="auth-user-1",
            username="authuser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        db.refresh(user)
        db.refresh(identity)
        return AuthUser(user_id=user.id, identity_id=identity.id)


@pytest.fixture
def _mock_session(sentry_app, auth_user):
    """Mock the session so any cookie-bearing request is authenticated as auth_user."""
    data = SessionData(
        user_id=str(auth_user.id),
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    sessions = sentry_app.web.sessions

    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def sentry_client(sentry_app, _mock_session):
    """Test client with authentication."""
    return TestClient(
        sentry_app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session"},
    )


class IdHolder:
    """Holds an ID to avoid detached ORM instance issues."""

    def __init__(self, id):
        self.id = id


@pytest.fixture
def workspace(sentry_app, auth_user):
    """Create a test workspace with membership for the auth user."""
    with sentry_app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-workspace", slug="test-workspace")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=auth_user.id)
        return IdHolder(id=ws.id)


@pytest.fixture
def sentry_org(sentry_app, workspace, auth_user):
    """Create a test Sentry org linked to workspace with membership for the auth user."""
    with sentry_app.database.session() as db:
        org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="my-sentry-org",
            external_org_id="my-sentry-org",
            provider="sentry",
            installation_id="sentry-install-uuid",
        )
        db.add(
            OrgMembership(
                org_id=org.id,
                provider_identity_id=auth_user.identity_id,
                role="owner",
            )
        )
        db.commit()
        return org.id


@pytest.fixture
def unlinked_sentry_org(sentry_app):
    """Create an unlinked Sentry org (no workspace)."""
    with sentry_app.database.session() as db:
        return db_create_org(
            db=db,
            workspace_id=None,
            name="unlinked-org",
            external_org_id="unlinked-org",
            provider="sentry",
            installation_id="sentry-install-unlinked",
        )


# -- Link endpoint tests ------------------------------------------------------


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_link_sentry_source_links_unlinked_org(
    mock_broker, sentry_client, sentry_app, workspace, unlinked_sentry_org
):
    """POST /sources/sentry links an unlinked Organization to a workspace."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="1", slug="unlinked-org", name="Unlinked Org")
    )

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": str(workspace.id),
            "org_slug": "unlinked-org",
            "auth_token": "sntrys_test_token_123",
            "client_secret": "test-client-secret",
            "base_url": "https://sentry.io",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["org_slug"] == "unlinked-org"
    assert data["projects_synced"] is True

    # Verify Organization is now linked with encrypted credentials
    with sentry_app.database.session() as db:
        org = db_get_org_by_external_id(db, "unlinked-org", provider="sentry")
        assert org.workspace_id == workspace.id
        assert org.auth_token_encrypted is not None
        assert org.base_url == "https://sentry.io"


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_link_sentry_source_creates_when_no_webhook(
    mock_broker, sentry_client, sentry_app, workspace
):
    """POST /sources/sentry creates Organization directly if webhook hasn't fired."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="1", slug="new-org", name="New Org")
    )

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": str(workspace.id),
            "org_slug": "new-org",
            "auth_token": "sntrys_test_token_123",
            "client_secret": "test-client-secret",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["org_slug"] == "new-org"

    with sentry_app.database.session() as db:
        org = db_get_org_by_external_id(db, "new-org", provider="sentry")
        assert org is not None
        assert org.workspace_id == workspace.id
        assert org.auth_token_encrypted is not None


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_link_sentry_source_idempotent(
    mock_broker, sentry_client, sentry_app, workspace, sentry_org
):
    """POST /sources/sentry is idempotent when already linked to same workspace."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="1", slug="my-sentry-org", name="My Org")
    )

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": str(workspace.id),
            "org_slug": "my-sentry-org",
            "auth_token": "sntrys_test_token_123",
            "client_secret": "test-client-secret",
        },
    )

    assert response.status_code == 200


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_link_sentry_source_cross_workspace_conflict(
    mock_broker, sentry_client, sentry_app, sentry_org
):
    """POST /sources/sentry returns 409 when org is linked to another workspace."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="1", slug="my-sentry-org", name="My Org")
    )

    # Create a second workspace with membership for the auth user
    with sentry_app.database.session() as db:
        ws2 = db_create_workspace(db=db, name="other-ws", slug="other-ws")
        # Need auth_user id — get it from the sentry_org fixture's workspace membership
        from api.models.workspaces import WorkspaceMembership

        existing = db.query(WorkspaceMembership).first()
        db_create_workspace_membership(db=db, workspace_id=ws2.id, user_id=existing.user_id)
        ws2_id = str(ws2.id)

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": ws2_id,
            "org_slug": "my-sentry-org",
            "auth_token": "sntrys_test_token_123",
            "client_secret": "test-client-secret",
        },
    )

    assert response.status_code == 409
    assert "another workspace" in response.json()["detail"]


def test_link_sentry_source_workspace_not_found(sentry_client, sentry_app):
    """POST /sources/sentry returns 404 for unknown workspace."""
    sentry_app.sentry.list_organizations = AsyncMock(
        return_value=[SentryOrganization(id="1", slug="org", name="Org")]
    )

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": "00000000-0000-0000-0000-000000000000",
            "org_slug": "test-org",
            "auth_token": "sntrys_test_token_123",
            "client_secret": "test-client-secret",
        },
    )

    assert response.status_code == 404


# -- Project sync consumer tests -----------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.consumer.get_current_app")
async def test_sync_sentry_projects_consumer(mock_get_app, sentry_app):
    """Consumer syncs Sentry projects into the database."""
    from api.routers.sources.sentry.consumer import sync_sentry_projects
    from api.routers.sources.sentry.schemas import SentrySyncProjectsMessage

    with sentry_app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-workspace", slug="test-ws-consumer")
        created_org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="test-sentry-org",
            external_org_id="my-org",
            provider="sentry",
            installation_id="install-99",
        )
        org_id = str(created_org.id)

    mock_get_app.return_value = sentry_app
    sentry_app.sentry.list_projects = AsyncMock(
        return_value=[
            SentryProjectAPI(id="100", slug="frontend", name="Frontend"),
            SentryProjectAPI(id="101", slug="backend-api", name="Backend API"),
        ]
    )

    await sync_sentry_projects(SentrySyncProjectsMessage(org_id=org_id, org_slug="my-org"))

    with sentry_app.database.session() as db:
        projects = db_get_repositories_by_org(db, UUID(org_id))
        assert len(projects) == 2
        slugs = {p.name for p in projects}
        assert slugs == {"frontend", "backend-api"}

    sentry_app.sentry.list_projects.assert_called_once_with(
        "my-org", auth_token=None, base_url=None
    )


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.consumer.get_current_app")
async def test_sync_sentry_projects_idempotent(mock_get_app, sentry_app):
    """Re-running sync upserts projects without duplicates."""
    from api.routers.sources.sentry.consumer import sync_sentry_projects
    from api.routers.sources.sentry.schemas import SentrySyncProjectsMessage

    with sentry_app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-workspace", slug="test-ws-idempotent")
        created_org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="test-sentry-org",
            external_org_id="my-org",
            provider="sentry",
            installation_id="install-99",
        )
        org_id = str(created_org.id)

    mock_get_app.return_value = sentry_app
    sentry_app.sentry.list_projects = AsyncMock(
        return_value=[
            SentryProjectAPI(id="100", slug="frontend", name="Frontend"),
        ]
    )

    message = SentrySyncProjectsMessage(org_id=org_id, org_slug="my-org")

    await sync_sentry_projects(message)
    await sync_sentry_projects(message)

    with sentry_app.database.session() as db:
        projects = db_get_repositories_by_org(db, UUID(org_id))
        assert len(projects) == 1


# -- Project listing tests -----------------------------------------------------


def test_list_sentry_projects(sentry_client, sentry_app, sentry_org):
    """GET /sources/sentry/projects returns synced projects."""
    from api.database import db_upsert_repository

    with sentry_app.database.session() as db:
        db_upsert_repository(
            db=db,
            org_id=sentry_org,
            external_id="200",
            name="web-app",
            provider="sentry",
        )
        db_upsert_repository(
            db=db,
            org_id=sentry_org,
            external_id="201",
            name="api-server",
            provider="sentry",
        )

    response = sentry_client.get(
        "/sources/sentry/projects",
        params={"org_id": str(sentry_org)},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    names = {p["name"] for p in data}
    assert names == {"web-app", "api-server"}


def test_list_sentry_projects_empty(sentry_client, sentry_org):
    """GET /sources/sentry/projects with no projects returns empty list."""
    response = sentry_client.get(
        "/sources/sentry/projects",
        params={"org_id": str(sentry_org)},
    )

    assert response.status_code == 200
    assert response.json() == []


# -- Backfill scope on link ----------------------------------------------------


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_link_sentry_source_stores_backfill_scope(
    mock_broker, sentry_client, sentry_app, workspace
):
    """The scope chosen at connect time is stored on the organization."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="9", slug="scope-org", name="Scope Org")
    )

    response = sentry_client.post(
        "/sources/sentry",
        json={
            "workspace_id": str(workspace.id),
            "org_slug": "scope-org",
            "auth_token": "tok",
            "client_secret": "secret",
            "backfill_scope": "none",
        },
    )

    assert response.status_code == 200
    with sentry_app.database.session() as db:
        org = db_get_org_by_external_id(db, "scope-org", provider="sentry")
        assert org is not None
        assert org.settings["backfill"] == "none"


@patch("api.routers.sources.sentry.route.get_faststream_broker")
def test_relinking_without_scope_keeps_stored_choice(
    mock_broker, sentry_client, sentry_app, workspace
):
    """Rotating credentials must not silently re-enable a switched-off import."""
    mock_broker.return_value = AsyncMock()
    sentry_app.sentry.get_organization = AsyncMock(
        return_value=SentryOrganization(id="10", slug="relink-org", name="Relink Org")
    )

    body = {
        "workspace_id": str(workspace.id),
        "org_slug": "relink-org",
        "auth_token": "tok",
        "client_secret": "secret",
    }
    first = sentry_client.post("/sources/sentry", json={**body, "backfill_scope": "none"})
    assert first.status_code == 200

    # Relink with a new token and no scope in the payload
    second = sentry_client.post("/sources/sentry", json={**body, "auth_token": "rotated"})
    assert second.status_code == 200

    with sentry_app.database.session() as db:
        org = db_get_org_by_external_id(db, "relink-org", provider="sentry")
        assert org is not None
        assert org.settings["backfill"] == "none"
