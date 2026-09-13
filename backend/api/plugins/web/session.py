"""Redis-backed server-side session manager.

Sessions are stored as Redis keys (`session:{uuid}`) with sliding-window TTL.
The cookie only holds an opaque session UUID — no sensitive data.

This is not a standalone plugin; it lives inside WebServerPlugin and is
accessible via ``app.web.sessions``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from api.context import get_current_app
from api.plugins.web.config import SessionConfig

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

SESSION_PREFIX = "session:"

# The inactivity TTL is measured in hours, so rewriting the key on every
# request buys nothing and adds a Redis write to each one.
REFRESH_INTERVAL_SECONDS = 300


class SessionData(BaseModel):
    """Data stored in Redis for each session."""

    user_id: str
    created_at: str
    last_active: str


class SessionManager:
    """Redis-backed session manager with sliding-window expiry."""

    def __init__(self, config: SessionConfig) -> None:
        self.config = config

    def _get_redis(self):  # type: ignore[no-untyped-def]
        """Get Redis client from FastStream plugin."""
        app = get_current_app()
        if not app.faststream:
            raise RuntimeError("FastStream plugin not available")
        return app.faststream.get_redis()

    def _inactivity_ttl(self) -> int:
        """Get inactivity timeout in seconds."""
        return self.config.inactivity_timeout_hours * 3600

    async def create(self, user_id: UUID) -> str:
        """Create a new session. Returns opaque session ID."""
        redis = self._get_redis()
        session_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        data = SessionData(user_id=str(user_id), created_at=now, last_active=now)

        key = f"{SESSION_PREFIX}{session_id}"
        await redis.set(key, data.model_dump_json(), ex=self._inactivity_ttl())
        return session_id

    async def get(self, session_id: str) -> SessionData | None:
        """Look up a session by ID. Returns None if expired or missing."""
        redis = self._get_redis()
        key = f"{SESSION_PREFIX}{session_id}"
        raw = await redis.get(key)

        if not raw:
            return None

        data = SessionData(**json.loads(raw))

        created = datetime.fromisoformat(data.created_at)
        max_seconds = self.config.max_lifetime_days * 86400
        if (datetime.now(UTC) - created).total_seconds() > max_seconds:
            await self.delete(session_id)
            return None

        return data

    async def refresh(self, session_id: str, data: SessionData) -> None:
        """Slide the inactivity TTL, at most once per ``REFRESH_INTERVAL_SECONDS``.

        Takes the data ``get`` just returned, so refreshing costs no extra read.
        """
        now = datetime.now(UTC)
        idle = (now - datetime.fromisoformat(data.last_active)).total_seconds()
        if idle < REFRESH_INTERVAL_SECONDS:
            return

        refreshed = data.model_copy(update={"last_active": now.isoformat()})
        redis = self._get_redis()
        # xx: a session logged out since it was read must stay gone.
        await redis.set(
            f"{SESSION_PREFIX}{session_id}",
            refreshed.model_dump_json(),
            ex=self._inactivity_ttl(),
            xx=True,
        )

    async def delete(self, session_id: str) -> None:
        """Delete a session (logout)."""
        redis = self._get_redis()
        await redis.delete(f"{SESSION_PREFIX}{session_id}")

    async def delete_all_for_user(self, user_id: UUID) -> None:
        """Revoke all sessions for a user."""
        redis = self._get_redis()
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{SESSION_PREFIX}*", count=100)
            for key in keys:
                raw = await redis.get(key)
                if raw:
                    data = SessionData(**json.loads(raw))
                    if data.user_id == str(user_id):
                        await redis.delete(key)
            if cursor == 0:
                break
