"""SSE event publishers.

Helper functions to publish events to Redis channels for SSE streaming.
The webhook/business logic consumer calls these instead of broadcasting
directly to sse_manager, so events cross instance boundaries via Redis.

All events are scoped by workspace_id so clients only receive events
for the workspace they are subscribed to.
"""

import logging
from typing import Any

from api.context import get_current_app
from api.routers.stream.schemas import SSEEventMessage
from api.sse.schemas import EventType
from api.sse.stats_cache import invalidate_stats

logger = logging.getLogger(__name__)

SSE_CHANNEL = "jeanclode.events.sse"


async def publish_issue_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish an issue event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this issue belongs to.
        action: What happened (created, updated).
        payload: Issue-specific data (issue_id, status, title, etc.).
    """
    await _publish(EventType.ISSUE, action, workspace_id, payload)


async def publish_pull_request_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish a pull request event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this PR belongs to.
        action: What happened (created, updated).
        payload: PR-specific data (pull_request_id, state, pr_url, etc.).
    """
    await _publish(EventType.PULL_REQUEST, action, workspace_id, payload)


async def publish_execution_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish an execution event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this execution belongs to.
        action: What happened (created, updated).
        payload: Execution-specific data (execution_id, status, issue_id, etc.).
    """
    await _publish(EventType.EXECUTION, action, workspace_id, payload)


async def publish_mapping_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish a mapping event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this mapping belongs to.
        action: What happened (resolved).
        payload: Mapping-specific data (mapped, total, etc.).
    """
    await _publish(EventType.MAPPING, action, workspace_id, payload)


async def publish_backfill_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish a backfill progress event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this backfill belongs to.
        action: What happened (completed, skipped).
        payload: Backfill-specific data (created, skipped counts, scope).
    """
    await _publish(EventType.BACKFILL, action, workspace_id, payload)


async def publish_sync_event(
    workspace_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Publish an integration sync event to the SSE Redis channel.

    Args:
        workspace_id: Workspace this sync belongs to.
        action: What happened (repos_synced, projects_synced, org_connected).
        payload: Sync-specific data (org_id, count, etc.).
    """
    await _publish(EventType.SYNC, action, workspace_id, payload)


async def _publish(
    event_type: EventType,
    action: str,
    workspace_id: str,
    payload: dict[str, Any],
) -> None:
    """Publish an SSE event to the Redis channel."""
    # Before publishing: a client refetching on this event must miss the cache.
    await invalidate_stats(workspace_id)
    try:
        app = get_current_app()
        if not app.faststream:
            logger.warning("FastStream not available, SSE event not published")
            return

        message = SSEEventMessage(
            event_type=event_type,
            action=action,
            workspace_id=workspace_id,
            payload=payload,
        )
        await app.faststream.publish(SSE_CHANNEL, message.model_dump())
        logger.debug(f"Published {event_type} {action} event for workspace {workspace_id}")
    except Exception as e:
        logger.error(f"Failed to publish {event_type} event: {e}", exc_info=True)
