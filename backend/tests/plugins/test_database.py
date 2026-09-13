"""Tests for the DatabasePlugin."""

import pytest
from sqlalchemy.orm import Session

from api.app import Application
from api.plugins.database.config import DatabasePluginConfig
from api.plugins.database.plugin import DatabasePlugin


def test_startup_creates_engine(app: Application) -> None:
    db = app.database
    assert db is not None
    assert db.engine is not None
    assert db.SessionLocal is not None


def test_get_session_returns_session(app: Application) -> None:
    db = app.database
    assert db is not None
    session = db.get_session()
    assert isinstance(session, Session)
    session.close()


def test_get_session_before_startup_raises() -> None:
    plugin = DatabasePlugin(DatabasePluginConfig(enabled=True, url="sqlite:///:memory:"))
    with pytest.raises(RuntimeError, match="DatabasePlugin not started"):
        plugin.get_session()


def test_session_context_manager(app: Application) -> None:
    db = app.database
    assert db is not None
    with db.session() as session:
        assert isinstance(session, Session)


async def test_shutdown_disposes_engine(app: Application) -> None:
    db = app.database
    assert db is not None
    assert db.engine is not None
    await db.shutdown()
    assert db.engine is None
    assert db.SessionLocal is None


async def test_health_check_healthy(app: Application) -> None:
    db = app.database
    assert db is not None
    result = await db.health_check()
    assert result == {"healthy": True}


async def test_health_check_unhealthy() -> None:
    plugin = DatabasePlugin(DatabasePluginConfig(enabled=True, url="sqlite:///:memory:"))
    result = await plugin.health_check()
    assert result["healthy"] is False
    assert "error" in result
