"""Tests for the WebServerPlugin."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from api.app import Application
from api.plugins.web.config import WebPluginConfig
from api.plugins.web.plugin import WebServerPlugin


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["environment"] == "development"


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert "plugins" in data


async def test_health_endpoint_unhealthy(app: Application) -> None:
    web = app.web
    assert web is not None
    assert web.app is not None

    with patch.object(app, "health_check", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = {
            "healthy": False,
            "plugins": {"database": {"healthy": False, "error": "down"}},
        }
        test_client = TestClient(web.get_asgi_app())
        response = test_client.get("/health")
        assert response.status_code == 503
        assert response.json()["healthy"] is False


def test_error_handling_middleware(client: TestClient) -> None:
    # Add a route that raises an unhandled exception
    app = client.app  # type: ignore[attr-defined]
    router = APIRouter()

    @router.get("/test-error")
    async def error_route() -> None:
        raise ValueError("test error")

    app.include_router(router)  # type: ignore[union-attr]

    response = client.get("/test-error")
    assert response.status_code == 500
    data = response.json()
    assert data["error"] == "internal_server_error"


def test_cors_headers(client: TestClient) -> None:
    response = client.options(
        "/",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" in response.headers


def test_get_asgi_app_before_startup_raises() -> None:
    plugin = WebServerPlugin(
        WebPluginConfig(
            enabled=True,
            admin={"secret": "test-admin-secret-value-at-least-32-chars-long"},
        )
    )
    with pytest.raises(RuntimeError, match="WebServerPlugin not started"):
        plugin.get_asgi_app()


async def test_health_check_reports_server_status(app: Application) -> None:
    web = app.web
    assert web is not None
    result = await web.health_check()
    assert result["healthy"] is True
    # skip_server=true so no background task
    assert result["server_running"] is False
