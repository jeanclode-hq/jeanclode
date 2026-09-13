"""FastStream consumers for GitLab group-level and system hooks.

Both streams carry the same envelope — ``{"instance_url", "payload"}`` — and
differ only in which hook produced it, which decides how much the payload is
trusted (see ``system_hook``).
"""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app

from .group_hook import handle_group_hook_event
from .system_hook import handle_system_hook_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub("jeanclode.events.gitlab.group", group="jeanclode", consumer="worker-1")
)
async def consume_gitlab_group_hook(event: dict[str, Any]) -> None:
    """Consume a group-level GitLab hook event from the Redis Stream."""
    app = get_current_app()
    if not app.database or not app.gitlab:
        logger.error("Database or GitLab plugin not configured")
        return

    result = await handle_group_hook_event(event, app.database, app.gitlab)
    logger.debug(f"GitLab group hook consumer: {result.message}")


@router.subscriber(
    stream=StreamSub("jeanclode.events.gitlab.system", group="jeanclode", consumer="worker-1")
)
async def consume_gitlab_system_hook(event: dict[str, Any]) -> None:
    """Consume an instance-wide GitLab system hook event from the Redis Stream."""
    app = get_current_app()
    if not app.database or not app.gitlab:
        logger.error("Database or GitLab plugin not configured")
        return

    result = await handle_system_hook_event(event, app.database, app.gitlab)
    logger.debug(f"GitLab system hook consumer: {result.message}")
