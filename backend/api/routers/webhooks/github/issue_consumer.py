"""FastStream consumer for GitHub issues webhooks."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .issues import handle_issue_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub("jeanclode.events.github.issues", group="jeanclode", consumer="worker-1")
)
async def consume_github_issue(event: dict[str, Any]) -> None:
    """Consume a GitHub issues webhook from the Redis Stream."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    result = await handle_issue_event(event)

    logger.debug(f"GitHub issue consumer: {result.message}")
