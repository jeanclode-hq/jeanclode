"""Event message schemas for SSE channels.

Pydantic models for Redis channel messages used by
consumers and domain routers.
"""

from typing import Any

from pydantic import BaseModel

from api.sse.schemas import EventType


class SSEEventMessage(BaseModel):
    """Message broadcast through the SSE stream.

    - event_type: resource type, used as the SSE `event` field
    - action: what happened (created, status_changed, completed, ...)
    - workspace_id: which workspace this event belongs to
    - payload: resource-specific data
    """

    event_type: EventType
    action: str
    workspace_id: str
    payload: dict[str, Any] = {}
