"""Tests for the manual per-org repository re-sync endpoint.

Webhooks are the normal path for new repos, but deliveries get dropped and a
GitLab instance may have no system hook at all — so every git org needs a
button that re-runs the sync that its connection ran.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.database import db_create_org, db_create_workspace, db_get_org_by_id
from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.users import User
from api.plugins.web.session import SessionData
from tests.utils.access import grant_org_access

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture
def sync_app(app):
    """App fixture with tables created."""
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
def auth_user(sync_app):
    """Create an authenticated user with a provider identity."""
    with sync_app.database.session() as db:
        user = User(email="sync@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="sync-user-1",
            username="syncuser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        db.refresh(user)
        db.refresh(identity)
        return AuthUser(user_id=user.id, identity_id=identity.id)


@pytest.fixture
def _mock_session(sync_app, auth_user):
    """Mock the session so any cookie-bearing request is authenticated."""
    data = SessionData(
        user_id=str(auth_user.id),
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    sessions = sync_app.web.sessions

    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def sync_client(sync_app, _mock_session):
    """Test client with authentication."""
    return TestClient(
        sync_app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session"},
    )


@pytest.fixture
def make_org(sync_app, auth_user):
    """Create an org the authenticated user is a member of."""

    def _make(provider: str, **kwargs):
        with sync_app.database.session() as db:
            workspace = db_create_workspace(
                db=db, name=f"ws-{provider}-{kwargs.get('name', 'x')}", slug=f"ws-{provider}"
            )
            org = db_create_org(
                db=db,
                workspace_id=workspace.id,
                name=kwargs.pop("name", f"{provider}-org"),
                external_org_id=kwargs.pop("external_org_id", "900"),
                provider=provider,
                **kwargs,
            )
            grant_org_access(db, org, auth_user.id)
            return org.id

    return _make


# -- Tests ---------------------------------------------------------------------


@patch("api.routers.organizations.route.get_faststream_broker")
def test_github_sync_queues_installation_sync(mock_broker, sync_client, make_org):
    """A GitHub org re-runs the installation sync, which upserts every repo."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org("github", installation_id="55", external_org_id="1")

    response = sync_client.post(f"/organizations/{org_id}/sync-repositories")

    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["provider"] == "github"

    kwargs = mock_broker_instance.publish.call_args.kwargs
    assert kwargs["stream"] == "jeanclode.events.github.sync_installation"
    assert mock_broker_instance.publish.call_args.args[0].installation_id == "55"


@patch("api.routers.organizations.route.get_faststream_broker")
def test_gitlab_sync_queues_group_sync(mock_broker, sync_client, sync_app, make_org):
    """A GitLab org re-runs the group project sync against its own group id."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org(
        "gitlab",
        external_org_id="42",
        installation_id="gitlab-group-42",
        auth_token_encrypted=sync_app.database.encrypt("group-token"),
    )

    response = sync_client.post(f"/organizations/{org_id}/sync-repositories")

    assert response.status_code == 202
    assert response.json()["provider"] == "gitlab"

    kwargs = mock_broker_instance.publish.call_args.kwargs
    assert kwargs["stream"] == "jeanclode.events.gitlab.sync_group_repositories"
    message = mock_broker_instance.publish.call_args.args[0]
    assert message.group_id == "42"
    assert message.org_id == str(org_id)


@patch("api.routers.organizations.route.get_faststream_broker")
def test_gitlab_subgroup_sync_uses_inherited_token(
    mock_broker, sync_client, sync_app, auth_user, make_org
):
    """A token-less subgroup org syncs on the token of the group above it."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    parent_id = make_org(
        "gitlab",
        external_org_id="42",
        installation_id="gitlab-group-42",
        auth_token_encrypted=sync_app.database.encrypt("group-token"),
    )

    with sync_app.database.session() as db:
        parent = db_get_org_by_id(db, parent_id)
        child = db_create_org(
            db=db,
            workspace_id=parent.workspace_id,
            name="subgroup",
            external_org_id="43",
            provider="gitlab",
            parent_org_id=parent.id,
            root_org_id=parent.id,
        )
        grant_org_access(db, child, auth_user.id)
        child_id = child.id

    response = sync_client.post(f"/organizations/{child_id}/sync-repositories")

    assert response.status_code == 202
    assert mock_broker_instance.publish.call_args.args[0].group_id == "43"


def test_gitlab_sync_without_token_is_rejected(sync_client, make_org):
    """No token anywhere in the hierarchy means there is nothing to sync with."""
    org_id = make_org("gitlab", external_org_id="77", installation_id="gitlab-group-77")

    response = sync_client.post(f"/organizations/{org_id}/sync-repositories")

    assert response.status_code == 400
    assert "token" in response.json()["detail"].lower()


def test_sentry_org_sync_is_rejected(sync_client, make_org):
    """Repository sync only means something for the git providers."""
    org_id = make_org("sentry", external_org_id="sentry-org", installation_id="sentry-1")

    response = sync_client.post(f"/organizations/{org_id}/sync-repositories")

    assert response.status_code == 400
    assert "sentry" in response.json()["detail"]


@patch("api.routers.organizations.route.get_faststream_broker")
def test_enabling_manage_project_webhooks_queues_a_group_sync(mock_broker, sync_client, make_org):
    """Flipping the setting on re-runs the sync so hooks get created now."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org("gitlab", external_org_id="88", installation_id="gitlab-group-88")

    response = sync_client.patch(
        f"/organizations/{org_id}/settings",
        json={"manage_project_webhooks": True},
    )

    assert response.status_code == 200
    assert response.json()["manage_project_webhooks"] is True
    assert (
        mock_broker_instance.publish.call_args.kwargs["stream"]
        == "jeanclode.events.gitlab.sync_group_repositories"
    )
    assert mock_broker_instance.publish.call_args.args[0].group_id == "88"


@patch("api.routers.organizations.route.get_faststream_broker")
def test_settings_patch_without_the_toggle_queues_nothing(mock_broker, sync_client, make_org):
    """An unrelated settings change must not kick off a sync."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org("gitlab", external_org_id="89", installation_id="gitlab-group-89")

    response = sync_client.patch(
        f"/organizations/{org_id}/settings",
        json={"triggers": {"review": "manual"}},
    )

    assert response.status_code == 200
    mock_broker_instance.publish.assert_not_called()


def _add_gitlab_repo(
    sync_app, org_id, external_id: str, *, auth_token_encrypted: str | None = None
):
    from api.database import db_create_repository

    with sync_app.database.session() as db:
        db_create_repository(
            db=db,
            org_id=org_id,
            external_id=external_id,
            name=f"grp/{external_id}",
            provider="gitlab",
            provider_url="https://gitlab.example.dev",
            auth_token_encrypted=auth_token_encrypted,
        )


@patch("api.routers.organizations.route.get_faststream_broker")
def test_disabling_manage_project_webhooks_queues_a_teardown(
    mock_broker, sync_client, sync_app, make_org
):
    """Turning the setting off queues deletion of the hooks we created."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org(
        "gitlab",
        external_org_id="42",
        base_url="https://gitlab.example.dev",
        installation_id="gitlab-group-42",
        auth_token_encrypted=sync_app.database.encrypt("group-token"),
        settings={"manage_project_webhooks": True},
    )
    _add_gitlab_repo(sync_app, org_id, "101")
    _add_gitlab_repo(sync_app, org_id, "102")

    response = sync_client.patch(
        f"/organizations/{org_id}/settings", json={"manage_project_webhooks": False}
    )

    assert response.status_code == 200
    assert response.json()["manage_project_webhooks"] is False
    kwargs = mock_broker_instance.publish.call_args.kwargs
    assert kwargs["stream"] == "jeanclode.events.gitlab.project_hook_teardown"
    message = mock_broker_instance.publish.call_args.args[0]
    assert sorted(p.project_id for p in message.projects) == ["101", "102"]
    assert {p.provider_url for p in message.projects} == {"https://gitlab.example.dev"}
    assert {sync_app.database.decrypt(p.encrypted_token) for p in message.projects} == {
        "group-token"
    }


@patch("api.routers.organizations.route.get_faststream_broker")
def test_teardown_uses_the_repo_token_when_the_org_has_none(
    mock_broker, sync_client, sync_app, make_org
):
    """Repos added one-by-one with project tokens: the org is a token-less
    placeholder, so each project's own token must be used."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org(
        "gitlab",
        external_org_id="50",
        base_url="https://gitlab.example.dev",
        installation_id="gitlab-project-9001",
        settings={"manage_project_webhooks": True},
    )
    _add_gitlab_repo(
        sync_app, org_id, "501", auth_token_encrypted=sync_app.database.encrypt("tok-a")
    )
    _add_gitlab_repo(
        sync_app, org_id, "502", auth_token_encrypted=sync_app.database.encrypt("tok-b")
    )

    response = sync_client.patch(
        f"/organizations/{org_id}/settings", json={"manage_project_webhooks": False}
    )

    assert response.status_code == 200
    message = mock_broker_instance.publish.call_args.args[0]
    tokens = {p.project_id: sync_app.database.decrypt(p.encrypted_token) for p in message.projects}
    assert tokens == {"501": "tok-a", "502": "tok-b"}


@patch("api.routers.organizations.route.get_faststream_broker")
def test_deleting_a_gitlab_org_queues_hook_teardown(mock_broker, sync_client, sync_app, make_org):
    """Deleting an org with the setting on tears its project hooks down (async)."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org(
        "gitlab",
        external_org_id="42",
        installation_id="gitlab-group-42",
        auth_token_encrypted=sync_app.database.encrypt("group-token"),
        settings={"manage_project_webhooks": True},
    )
    _add_gitlab_repo(sync_app, org_id, "201")

    response = sync_client.delete(f"/organizations/{org_id}")

    assert response.status_code == 200
    kwargs = mock_broker_instance.publish.call_args.kwargs
    assert kwargs["stream"] == "jeanclode.events.gitlab.project_hook_teardown"
    assert [p.project_id for p in mock_broker_instance.publish.call_args.args[0].projects] == [
        "201"
    ]

    with sync_app.database.session() as db:
        assert db_get_org_by_id(db, org_id) is None


@patch("api.routers.organizations.route.get_faststream_broker")
def test_deleting_a_gitlab_org_without_the_setting_queues_nothing(
    mock_broker, sync_client, sync_app, make_org
):
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = make_org(
        "gitlab",
        external_org_id="43",
        installation_id="gitlab-group-43",
        auth_token_encrypted=sync_app.database.encrypt("group-token"),
    )
    _add_gitlab_repo(sync_app, org_id, "301")

    response = sync_client.delete(f"/organizations/{org_id}")

    assert response.status_code == 200
    mock_broker_instance.publish.assert_not_called()
