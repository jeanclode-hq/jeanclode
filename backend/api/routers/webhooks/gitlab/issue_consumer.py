"""FastStream consumer for GitLab issue webhooks."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .issues import handle_issue_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub("jeanclode.events.gitlab.issues", group="jeanclode", consumer="worker-1")
)
async def consume_gitlab_issue(event: dict[str, Any]) -> None:
    """Consume a GitLab issue webhook from the Redis Stream."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    result = await handle_issue_event(event)

    logger.debug(f"GitLab issue consumer: {result.message}")
