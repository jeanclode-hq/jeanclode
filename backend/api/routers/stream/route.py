"""SSE stream endpoint.

Single server-sent events connection per client, scoped to a workspace.
All events (issues, agents, etc.) flow through this one stream, tagged
with event_type so the frontend can route them.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from api.models import User
from api.routers.auth.dependencies import verify_workspace_access
from api.sse import EventType, make_sse_event, sse_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Stream"])


@router.get(
    "/stream",
    operation_id="stream_events",
    name="stream_events",
    summary="Stream real-time events for a workspace",
    description="Single SSE connection scoped to a workspace. "
    "Only events for the given workspace are delivered.",
    status_code=200,
)
async def stream_events(
    workspace_id: str = Query(..., description="Workspace ID to subscribe to"),
    current_user: User = Depends(verify_workspace_access),
) -> EventSourceResponse:
    """SSE endpoint for streaming workspace events to a client."""
    client_id = str(uuid.uuid4())

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        queue = sse_manager.register_client(workspace_id, client_id)

        try:
            yield make_sse_event(
                EventType.CONNECTED,
                {
                    "client_id": client_id,
                    "workspace_id": workspace_id,
                    "message": "Stream connected",
                },
            )

            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30.0)
                except TimeoutError:
                    yield make_sse_event(EventType.KEEPALIVE, {})
                    continue

                if event_data.get("__sse_shutdown__"):
                    logger.debug(f"SSE shutdown signal received for client {client_id}")
                    break

                # Copy before mutating — broadcast_to_resource shares
                # the same dict reference across multiple client queues
                event_data = dict(event_data)
                event_type = event_data.pop("event_type", "message")
                yield make_sse_event(event_type, event_data)

        except asyncio.CancelledError:
            logger.debug(f"SSE client disconnected: {client_id}")
            raise
        except Exception as e:
            logger.error(f"SSE error for client {client_id}: {e}", exc_info=True)
            yield make_sse_event(EventType.ERROR, {"error": str(e)})
        finally:
            sse_manager.unregister_client(workspace_id, client_id, queue)

    return EventSourceResponse(event_generator())
