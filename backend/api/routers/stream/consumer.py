"""FastStream consumer for SSE fan-out.

Subscribes to the ``jeanclode.events.sse`` Redis channel and broadcasts
events only to clients subscribed to the matching workspace_id.
This is the bridge between Redis (cross-instance) and local asyncio queues.
"""

import logging

from faststream.redis import RedisRouter

from api.routers.stream.schemas import SSEEventMessage
from api.sse import sse_manager

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber("jeanclode.events.sse")
async def handle_sse_event(message: SSEEventMessage) -> None:
    """Receive an SSE event from Redis and fan out to workspace clients."""
    workspace_id = message.workspace_id

    client_count = sse_manager.get_client_count(resource_type=workspace_id)
    if client_count == 0:
        logger.debug(f"No SSE clients for workspace {workspace_id}, skipping fan-out")
        return

    event_data = message.model_dump()

    # Broadcast to all clients registered for this workspace
    for client_id in _get_workspace_client_ids(workspace_id):
        await sse_manager.broadcast_to_resource(workspace_id, client_id, event_data)

    logger.debug(
        f"Fanned out {message.event_type} event to {client_count} clients in workspace {workspace_id}"
    )


def _get_workspace_client_ids(workspace_id: str) -> list[str]:
    """Get all client IDs subscribed to a workspace."""
    if workspace_id not in sse_manager._resources:
        return []
    return list(sse_manager._resources[workspace_id].keys())
