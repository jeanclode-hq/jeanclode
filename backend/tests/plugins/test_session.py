"""Tests for SessionManager's throttled sliding-window refresh."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from api.plugins.web.config import SessionConfig
from api.plugins.web.session import (
    REFRESH_INTERVAL_SECONDS,
    SESSION_PREFIX,
    SessionData,
    SessionManager,
)


def _data(idle_seconds: float) -> SessionData:
    now = datetime.now(UTC)
    return SessionData(
        user_id="user-1",
        created_at=(now - timedelta(days=1)).isoformat(),
        last_active=(now - timedelta(seconds=idle_seconds)).isoformat(),
    )


@pytest.fixture
def redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def sessions(redis: AsyncMock) -> Iterator[SessionManager]:
    manager = SessionManager(SessionConfig())
    with patch.object(manager, "_get_redis", return_value=redis):
        yield manager


async def test_refresh_within_interval_touches_nothing(
    sessions: SessionManager, redis: AsyncMock
) -> None:
    await sessions.refresh("sid", _data(idle_seconds=REFRESH_INTERVAL_SECONDS - 60))

    redis.get.assert_not_called()
    redis.set.assert_not_called()


async def test_refresh_rewrites_idle_session_without_reading(
    sessions: SessionManager, redis: AsyncMock
) -> None:
    data = _data(idle_seconds=REFRESH_INTERVAL_SECONDS + 60)

    await sessions.refresh("sid", data)

    redis.get.assert_not_called()
    redis.set.assert_awaited_once()
    args, kwargs = redis.set.call_args
    assert args[0] == f"{SESSION_PREFIX}sid"
    # xx=True: a session deleted by logout in the meantime must not come back.
    assert kwargs == {"ex": SessionConfig().inactivity_timeout_hours * 3600, "xx": True}
    written = SessionData.model_validate_json(args[1])
    assert written.created_at == data.created_at
    assert written.last_active > data.last_active
