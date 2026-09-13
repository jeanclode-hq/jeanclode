"""SSE event type enumeration.

Defines the canonical set of SSE event types used across the system.
These map directly to the SSE `event` field on the wire.
"""

from enum import StrEnum


class EventType(StrEnum):
    """SSE resource event types.

    Used as the SSE `event` field. Each value represents a resource type.
    The action (created, updated, etc.) is carried in the payload.
    """

    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    EXECUTION = "execution"
    AGENT = "agent"
    MAPPING = "mapping"
    BACKFILL = "backfill"
    SYNC = "sync"

    # System
    CONNECTED = "connected"
    KEEPALIVE = "keepalive"
    ERROR = "error"
