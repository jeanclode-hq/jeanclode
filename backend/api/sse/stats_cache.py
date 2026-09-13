"""Per-workspace cache of the dashboard stats, invalidated by SSE events.

Every issue / execution / PR state change publishes an SSE event, so the
publisher bumps a per-workspace version and a cached value only counts while
its version is still current. The version is read *before* the stats query,
so an event landing mid-query leaves the freshly stored value already stale
instead of pinning pre-change counts until the TTL runs out.
"""

import json
import logging
from typing import Any

from api.context import get_current_app

logger = logging.getLogger(__name__)

# Bounds staleness for state changes that don't publish an event.
STATS_TTL_SECONDS = 15
# Outlives any cached value by far; an expired version reads as "0", which no
# value stored after an event carries.
VERSION_TTL_SECONDS = 24 * 3600


def _version_key(workspace_id: str) -> str:
    return f"stats:version:{workspace_id}"


def _value_key(workspace_id: str) -> str:
    return f"stats:value:{workspace_id}"


def _redis():  # type: ignore[no-untyped-def]
    app = get_current_app()
    if not app.faststream:
        raise RuntimeError("FastStream plugin not available")
    return app.faststream.get_redis()


def _decode(raw: Any) -> str | None:
    if isinstance(raw, bytes):
        return raw.decode()
    if isinstance(raw, str):
        return raw
    return None


async def get_cached_stats(workspace_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(stats or None, version to store fresh stats under)``.

    A ``None`` version means Redis couldn't be read, so nothing should be stored.
    """
    try:
        version_raw, value_raw = await _redis().mget(
            _version_key(workspace_id), _value_key(workspace_id)
        )
        version = _decode(version_raw) or "0"
        value = _decode(value_raw)
        if value is not None:
            cached = json.loads(value)
            if cached.get("version") == version:
                return cached["stats"], version
        return None, version
    except Exception:
        logger.warning("Stats cache read failed for workspace %s", workspace_id, exc_info=True)
        return None, None


async def store_stats(workspace_id: str, version: str, stats: dict[str, Any]) -> None:
    try:
        await _redis().set(
            _value_key(workspace_id),
            json.dumps({"version": version, "stats": stats}),
            ex=STATS_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Stats cache write failed for workspace %s", workspace_id, exc_info=True)


async def invalidate_stats(workspace_id: str) -> None:
    if not workspace_id:
        return
    try:
        redis = _redis()
        await redis.incr(_version_key(workspace_id))
        await redis.expire(_version_key(workspace_id), VERSION_TTL_SECONDS)
    except Exception:
        logger.warning(
            "Stats cache invalidation failed for workspace %s", workspace_id, exc_info=True
        )
