"""FastStream consumer for GitLab merge_request webhooks."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .merge_requests import handle_merge_request_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.merge_requests", group="jeanclode", consumer="worker-1"
    )
)
async def consume_gitlab_merge_request(event: dict[str, Any]) -> None:
    """Consume a GitLab merge_request webhook from the Redis Stream."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    result = await handle_merge_request_event(event)

    logger.debug(f"GitLab MR consumer: {result.message}")
