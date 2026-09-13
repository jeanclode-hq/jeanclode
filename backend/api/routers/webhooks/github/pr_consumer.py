"""FastStream consumer for GitHub pull_request webhooks."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .pull_requests import handle_pull_request_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.github.pull_requests", group="jeanclode", consumer="worker-1"
    )
)
async def consume_github_pull_request(event: dict[str, Any]) -> None:
    """Consume a GitHub pull_request webhook from the Redis Stream."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    result = await handle_pull_request_event(event)

    logger.debug(f"GitHub PR consumer: {result.message}")
