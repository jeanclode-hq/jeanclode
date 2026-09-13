"""Tests for auth endpoints — session plugin, route protection, /me, /logout."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.models import Base, ProviderIdentity, User
from api.plugins.web.session import SessionData


@pytest.fixture
def _tables(app):
    """Create and drop tables around each test."""
    db_plugin = app.database
    assert db_plugin is not None
    Base.metadata.create_all(bind=db_plugin.engine)
    yield
    Base.metadata.drop_all(bind=db_plugin.engine)


@pytest.fixture
def db_session(app, _tables):
    """Provide a database session for tests."""
    session = app.database.get_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_user(db_session):
    """Create a test user with a GitHub identity."""
    user = User(email="test@example.com")
    db_session.add(user)
    db_session.flush()

    identity = ProviderIdentity(
        provider="github",
        external_id="12345",
        username="testuser",
        avatar_url="https://example.com/avatar.png",
        user_id=user.id,
    )
    db_session.add(identity)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def _mock_session(app, test_user):
    """Patch the session plugin so any cookie is authenticated as test_user."""
    session_plugin = app.web.sessions
    assert session_plugin is not None

    data = SessionData(
        user_id=str(test_user.id),
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    with (
        patch.object(session_plugin, "get", new_callable=AsyncMock, return_value=data),
        patch.object(session_plugin, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def auth_test_client(app, _mock_session):
    """Test client with auth pre-configured."""
    return TestClient(
        app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session-id"},
    )


def test_get_me_unauthenticated(client):
    """GET /auth/me returns 401 without session cookie."""
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_get_me_authenticated(auth_test_client, test_user):
    """GET /auth/me returns user profile with valid session."""
    response = auth_test_client.get("/auth/me")
    assert response.status_code == 200

    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["display_username"] == "testuser"
    assert data["github_username"] == "testuser"
    assert data["github_external_id"] == "12345"
    assert data["id"] == str(test_user.id)


def test_get_me_expired_session(app, _tables, client):
    """GET /auth/me returns 401 with expired/invalid session."""
    session_plugin = app.web.sessions
    assert session_plugin is not None

    with patch.object(session_plugin, "get", new_callable=AsyncMock, return_value=None):
        response = client.get("/auth/me", cookies={"jeanclode_session": "expired-session"})
    assert response.status_code == 401


def test_logout(app, _tables, client):
    """POST /auth/logout deletes session and clears cookie."""
    session_plugin = app.web.sessions
    assert session_plugin is not None

    with patch.object(session_plugin, "delete", new_callable=AsyncMock) as mock_delete:
        response = client.post("/auth/logout", cookies={"jeanclode_session": "session-to-delete"})

    assert response.status_code == 200

    data = response.json()
    assert data["message"] == "Logged out successfully"

    mock_delete.assert_called_once_with("session-to-delete")

    cookie_header = response.headers.get("set-cookie", "")
    assert "jeanclode_session" in cookie_header
    assert "Max-Age=0" in cookie_header


def test_protected_repos_unauthenticated(client):
    """GET /repos returns 401 without auth."""
    response = client.get("/repos?org_id=" + str(uuid4()))
    assert response.status_code == 401


def test_protected_sentry_projects_unauthenticated(client):
    """GET /sources/sentry/projects returns 401 without auth."""
    response = client.get("/sources/sentry/projects?org_id=" + str(uuid4()))
    assert response.status_code == 401


def test_update_profile(auth_test_client, test_user, db_session):
    """PATCH /auth/me updates email."""
    response = auth_test_client.patch(
        "/auth/me",
        json={"email": "new@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["profile"]["email"] == "new@example.com"


def test_update_profile_invalid_email(auth_test_client, test_user):
    """PATCH /auth/me rejects invalid email format."""
    response = auth_test_client.patch(
        "/auth/me",
        json={"email": "not-an-email"},
    )
    assert response.status_code == 400


def test_disconnect_provider_last_identity(auth_test_client, test_user):
    """DELETE /auth/providers/{provider} blocks removal of last identity."""
    response = auth_test_client.delete("/auth/providers/github")
    assert response.status_code == 400
    assert "at least one" in response.json()["detail"].lower()


# =============================================================================
# _handle_login_callback: auto-join workspace
# =============================================================================


@pytest.fixture
def _login_tables(app):
    """Create and drop tables for login callback tests."""
    db_plugin = app.database
    assert db_plugin is not None
    Base.metadata.create_all(bind=db_plugin.engine)
    yield
    Base.metadata.drop_all(bind=db_plugin.engine)


@patch("api.routers.auth.route.handle_oauth_user_creation", new_callable=AsyncMock)
async def test_login_callback_sets_last_workspace_id_when_membership_exists(
    mock_create, app, _login_tables, mocker
):
    """Auto-join: last_workspace_id is set when the user has a workspace membership."""
    from api.database.workspace import db_create_workspace, db_create_workspace_membership
    from api.routers.auth.enums import OAuthProvider
    from api.routers.auth.route import _handle_login_callback

    with app.database.session() as db:
        user = User(email="autojoin@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="autojoin-99",
            username="autojoinuser",
            user_id=user.id,
        )
        db.add(identity)
        db.flush()
        ws = db_create_workspace(db=db, name="auto-ws", slug="auto-ws")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=user.id)
        db.commit()
        db.refresh(user)
        user_id = user.id
        ws_id = ws.id

    assert user.last_workspace_id is None
    mock_create.return_value = user
    mocker.patch.object(app.web.sessions, "create", new_callable=AsyncMock, return_value="sess-123")

    response = await _handle_login_callback(OAuthProvider.GITHUB, {})

    assert response.status_code == 302
    with app.database.session() as db:
        updated = db.query(User).filter(User.id == user_id).first()
        assert updated is not None
        assert updated.last_workspace_id == ws_id


@patch("api.routers.auth.route.handle_oauth_user_creation", new_callable=AsyncMock)
async def test_login_callback_preserves_valid_last_workspace_id(
    mock_create, app, _login_tables, mocker
):
    """Auto-join: an already-valid last_workspace_id is not overwritten."""
    from api.database.workspace import db_create_workspace, db_create_workspace_membership
    from api.routers.auth.enums import OAuthProvider
    from api.routers.auth.route import _handle_login_callback

    with app.database.session() as db:
        ws1 = db_create_workspace(db=db, name="ws-one", slug="ws-one")
        ws2 = db_create_workspace(db=db, name="ws-two", slug="ws-two")
        user = User(email="preserve@example.com", last_workspace_id=ws2.id)
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="preserve-99",
            username="preserveuser",
            user_id=user.id,
        )
        db.add(identity)
        db_create_workspace_membership(db=db, workspace_id=ws1.id, user_id=user.id)
        db_create_workspace_membership(db=db, workspace_id=ws2.id, user_id=user.id)
        db.commit()
        db.refresh(user)
        user_id = user.id
        ws2_id = ws2.id

    mock_create.return_value = user
    mocker.patch.object(app.web.sessions, "create", new_callable=AsyncMock, return_value="sess-456")

    await _handle_login_callback(OAuthProvider.GITHUB, {})

    with app.database.session() as db:
        updated = db.query(User).filter(User.id == user_id).first()
        assert updated is not None
        assert updated.last_workspace_id == ws2_id


@patch("api.routers.auth.route.handle_oauth_user_creation", new_callable=AsyncMock)
async def test_login_callback_updates_stale_last_workspace_id(
    mock_create, app, _login_tables, mocker
):
    """Auto-join: a stale last_workspace_id pointing to a non-member workspace is replaced."""
    from api.database.workspace import db_create_workspace, db_create_workspace_membership
    from api.routers.auth.enums import OAuthProvider
    from api.routers.auth.route import _handle_login_callback

    with app.database.session() as db:
        stale_ws = db_create_workspace(db=db, name="stale-ws", slug="stale-ws")
        real_ws = db_create_workspace(db=db, name="real-ws", slug="real-ws")
        user = User(email="stale@example.com", last_workspace_id=stale_ws.id)
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="stale-99",
            username="staleuser",
            user_id=user.id,
        )
        db.add(identity)
        db_create_workspace_membership(db=db, workspace_id=real_ws.id, user_id=user.id)
        db.commit()
        db.refresh(user)
        user_id = user.id
        real_ws_id = real_ws.id

    mock_create.return_value = user
    mocker.patch.object(app.web.sessions, "create", new_callable=AsyncMock, return_value="sess-789")

    await _handle_login_callback(OAuthProvider.GITHUB, {})

    with app.database.session() as db:
        updated = db.query(User).filter(User.id == user_id).first()
        assert updated is not None
        assert updated.last_workspace_id == real_ws_id


@patch("api.routers.auth.route.handle_oauth_user_creation", new_callable=AsyncMock)
async def test_login_callback_no_membership_leaves_last_workspace_id_unset(
    mock_create, app, _login_tables, mocker
):
    """Auto-join: when user has no memberships, last_workspace_id stays None."""
    from api.routers.auth.enums import OAuthProvider
    from api.routers.auth.route import _handle_login_callback

    with app.database.session() as db:
        user = User(email="nomember@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="nomember-99",
            username="nomemberuser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        db.refresh(user)
        user_id = user.id

    mock_create.return_value = user
    mocker.patch.object(app.web.sessions, "create", new_callable=AsyncMock, return_value="sess-000")

    await _handle_login_callback(OAuthProvider.GITHUB, {})

    with app.database.session() as db:
        updated = db.query(User).filter(User.id == user_id).first()
        assert updated is not None
        assert updated.last_workspace_id is None
