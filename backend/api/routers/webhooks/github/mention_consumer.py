"""FastStream consumer for queued GitHub @jeanclode mention events."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .mentions import handle_mention_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub("jeanclode.events.github.mentions", group="jeanclode", consumer="worker-1")
)
async def consume_github_mention(event: dict[str, Any]) -> None:
    """Consume a queued GitHub mention event.

    The producer wraps the raw GitHub payload in
    ``{"event_type": ..., "payload": {...}}`` so the handler can dispatch
    to the same normalizer regardless of which of three GitHub event
    types was received.
    """
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    event_type = event.get("event_type") or ""
    payload = event.get("payload") or {}
    if not event_type or not isinstance(payload, dict):
        logger.warning("Skipping malformed mention event: keys=%s", list(event.keys()))
        return

    result = await handle_mention_event(event_type, payload)
    logger.debug("GitHub mention consumer: %s", result.message)
