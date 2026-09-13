"""Tests for the Application class."""

from unittest.mock import AsyncMock, patch

from api.app import Application
from api.plugins.database.plugin import DatabasePlugin
from api.plugins.web.plugin import WebServerPlugin


def test_plugins_started_in_priority_order(app: Application) -> None:
    plugin_names = [p.plugin_name for p in app._plugins]
    # database=10, httpx(sentry)=30, web=50, faststream=60 (container disabled)
    assert plugin_names == ["database", "sentry", "web", "faststream"]


async def test_health_check_aggregates_plugins(app: Application) -> None:
    result = await app.health_check()
    assert result["healthy"] is True
    assert "plugins" in result
    assert "database" in result["plugins"]
    assert "web" in result["plugins"]
    assert "faststream" in result["plugins"]


async def test_health_check_reports_unhealthy(app: Application) -> None:
    db = app.database
    assert db is not None

    with patch.object(db, "health_check", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = {"healthy": False, "error": "connection refused"}
        result = await app.health_check()
        assert result["healthy"] is False
        assert result["plugins"]["database"]["healthy"] is False


def test_get_plugin_by_name(app: Application) -> None:
    db = app.get_plugin("database")
    assert isinstance(db, DatabasePlugin)

    web = app.get_plugin("web")
    assert isinstance(web, WebServerPlugin)

    missing = app.get_plugin("nonexistent")
    assert missing is None


async def test_shutdown_reverse_order(app: Application) -> None:
    shutdown_order: list[str] = []

    for plugin in app._plugins:
        original_shutdown = plugin.shutdown

        async def make_tracker(name: str, orig: object) -> None:
            shutdown_order.append(name)
            await orig()  # type: ignore[operator]

        plugin.shutdown = lambda name=plugin.plugin_name, orig=original_shutdown: make_tracker(
            name, orig
        )  # type: ignore[assignment]

    await app.shutdown()

    # Should be reverse of startup order
    assert shutdown_order == ["faststream", "web", "sentry", "database"]
