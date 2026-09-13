"""FastStream consumer for queued GitLab @jeanclode note events."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .notes import handle_note_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub("jeanclode.events.gitlab.mentions", group="jeanclode", consumer="worker-1")
)
async def consume_gitlab_note(event: dict[str, Any]) -> None:
    """Consume a queued GitLab note event."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    result = await handle_note_event(event)
    logger.debug("GitLab note consumer: %s", result.message)
