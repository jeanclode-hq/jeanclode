"""Tests for the FastStreamPlugin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio import BlockingConnectionPool

from api.plugins.faststream.config import FastStreamPluginConfig
from api.plugins.faststream.plugin import FastStreamPlugin


def _make_config(**overrides: object) -> FastStreamPluginConfig:
    defaults = {"enabled": True, "redis": {"url": "redis://localhost:6379", "db": 0}}
    defaults.update(overrides)  # type: ignore[arg-type]
    return FastStreamPluginConfig(**defaults)  # type: ignore[arg-type]


@patch("api.plugins.faststream.plugin.Redis.from_pool")
@patch("api.plugins.faststream.plugin.RedisBroker")
async def test_startup_creates_broker(
    mock_broker_cls: MagicMock, mock_from_pool: MagicMock
) -> None:
    mock_broker = MagicMock()
    mock_broker.start = AsyncMock()
    mock_broker_cls.return_value = mock_broker
    mock_from_pool.return_value = AsyncMock()

    plugin = FastStreamPlugin(_make_config())
    await plugin.startup()

    assert plugin._broker is not None
    mock_broker_cls.assert_called_once()


@patch("api.plugins.faststream.plugin.Redis.from_pool")
@patch("api.plugins.faststream.plugin.RedisBroker")
async def test_startup_uses_bounded_blocking_pool(
    mock_broker_cls: MagicMock, mock_from_pool: MagicMock
) -> None:
    """A burst past the pool must wait for a connection, not fail with MaxConnectionsError."""
    mock_from_pool.return_value = AsyncMock()
    mock_broker_cls.return_value = MagicMock(start=AsyncMock())

    config = _make_config(
        redis={"url": "redis://localhost:6379", "max_connections": 42, "pool_timeout": 3.0}
    )
    plugin = FastStreamPlugin(config)
    await plugin.startup()

    pool = mock_from_pool.call_args.args[0]
    assert isinstance(pool, BlockingConnectionPool)
    assert pool.max_connections == 42
    assert pool.timeout == 3.0


def test_get_broker_before_startup_raises() -> None:
    plugin = FastStreamPlugin(_make_config())
    with pytest.raises(RuntimeError, match="broker not initialized"):
        plugin.get_broker()


@patch("api.plugins.faststream.plugin.Redis.from_pool")
@patch("api.plugins.faststream.plugin.RedisBroker")
async def test_health_check_healthy(mock_broker_cls: MagicMock, mock_from_pool: MagicMock) -> None:
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_from_pool.return_value = mock_redis
    mock_broker_cls.return_value = MagicMock(start=AsyncMock())

    plugin = FastStreamPlugin(_make_config())
    await plugin.startup()

    result = await plugin.health_check()
    assert result == {"healthy": True}


@patch("api.plugins.faststream.plugin.Redis.from_pool")
@patch("api.plugins.faststream.plugin.RedisBroker")
async def test_startup_raises_when_redis_unreachable(
    mock_broker_cls: MagicMock, mock_from_pool: MagicMock
) -> None:
    """Queue is mandatory once enabled: startup must fail fast, not degrade silently."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("connection refused"))
    mock_from_pool.return_value = mock_redis
    mock_broker_cls.return_value = MagicMock(start=AsyncMock())

    plugin = FastStreamPlugin(_make_config())

    with pytest.raises(ConnectionError, match="connection refused"):
        await plugin.startup()


async def test_health_check_no_redis() -> None:
    plugin = FastStreamPlugin(_make_config())
    result = await plugin.health_check()
    assert result["healthy"] is False


@patch("api.plugins.faststream.plugin.Redis.from_pool")
@patch("api.plugins.faststream.plugin.RedisBroker")
async def test_shutdown_cleans_up(mock_broker_cls: MagicMock, mock_from_pool: MagicMock) -> None:
    mock_redis = AsyncMock()
    mock_from_pool.return_value = mock_redis
    mock_broker = MagicMock()
    mock_broker.start = AsyncMock()
    mock_broker.stop = AsyncMock()
    mock_broker_cls.return_value = mock_broker

    plugin = FastStreamPlugin(_make_config())
    await plugin.startup()
    await plugin.shutdown()

    assert plugin._broker is None
    assert plugin._redis is None
    mock_broker.stop.assert_called_once()
