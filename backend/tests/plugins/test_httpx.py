"""Tests for the HttpService and BaseHttpPlugin."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from api.plugins.httpx.service import HttpService

# ── HttpService Lifecycle ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_start_creates_client() -> None:
    service = HttpService(base_url="https://api.example.com")
    await service.on_start()
    assert service._client is not None
    await service.on_shutdown()


@pytest.mark.asyncio
async def test_on_shutdown_closes_client() -> None:
    service = HttpService(base_url="https://api.example.com")
    await service.on_start()
    assert service._client is not None
    await service.on_shutdown()
    assert service._client is None


def test_client_property_before_start_raises() -> None:
    service = HttpService()
    with pytest.raises(RuntimeError, match="not started"):
        _ = service.client


# ── HTTP Methods ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock)
async def test_get(mock_get: AsyncMock) -> None:
    service = HttpService(base_url="https://api.example.com")
    await service.on_start()
    mock_get.return_value = httpx.Response(
        200, json={"ok": True}, request=httpx.Request("GET", "https://api.example.com/test")
    )

    response = await service.get("/test")

    assert response.status_code == 200
    await service.on_shutdown()


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock)
async def test_post(mock_post: AsyncMock) -> None:
    service = HttpService(base_url="https://api.example.com")
    await service.on_start()
    mock_post.return_value = httpx.Response(
        201, json={"id": "1"}, request=httpx.Request("POST", "https://api.example.com/test")
    )

    response = await service.post("/test", json={"name": "test"})

    assert response.status_code == 201
    await service.on_shutdown()
