"""Root conftest - re-exports fixtures from utils."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from api.models import Base, ProviderIdentity, User
from api.plugins.web.session import SessionData
from tests.utils.app import app, client, test_config


@pytest.fixture
def db_session(app) -> Session:
    """Create tables and provide a database session for tests."""
    db_plugin = app.database
    assert db_plugin is not None

    # Drop ALL tables (including stale ones from old migrations) then create fresh
    from sqlalchemy import text

    with db_plugin.engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    Base.metadata.create_all(bind=db_plugin.engine)

    session = db_plugin.get_session()
    try:
        yield session
    finally:
        session.close()
        # Drop all tables after each test
        Base.metadata.drop_all(bind=db_plugin.engine)


class AuthUser:
    """Container for authenticated test user info (avoids detached session issues)."""

    def __init__(self, user_id: UUID, identity_id: UUID) -> None:
        self.id = user_id
        self.identity_id = identity_id


@pytest.fixture
def mock_auth(app) -> AuthUser:
    """Mock authentication for tests that need a valid user.

    Creates a test user with a provider identity and patches the session
    manager so that any request with the jeanclode_session cookie is
    authenticated.

    Returns an AuthUser with user_id and identity_id (not a detached ORM object).
    """
    db_plugin = app.database
    assert db_plugin is not None
    from sqlalchemy import text

    with db_plugin.engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    Base.metadata.create_all(bind=db_plugin.engine)

    with db_plugin.session() as db:
        user = User(email="testauth@example.com")
        db.add(user)
        db.flush()

        identity = ProviderIdentity(
            provider="github",
            external_id="auth-test-user-123",
            username="testauth",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        db.refresh(user)
        db.refresh(identity)
        auth_user = AuthUser(user_id=user.id, identity_id=identity.id)

    session_data = SessionData(
        user_id=str(auth_user.id),
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )

    sessions = app.web.sessions

    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=session_data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield auth_user

    Base.metadata.drop_all(bind=db_plugin.engine)


@pytest.fixture
def auth_client(app, mock_auth):
    """Test client with authentication pre-configured."""
    from fastapi.testclient import TestClient

    web = app.web
    assert web is not None
    tc = TestClient(web.get_asgi_app(), cookies={"jeanclode_session": "test-session"})
    return tc


__all__ = ["app", "auth_client", "client", "db_session", "mock_auth", "test_config"]
