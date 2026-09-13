"""SSE streaming utilities."""

import json
from typing import Any


def make_sse_event(event_type: str, data: dict[str, Any] | str) -> dict[str, str]:
    """Create SSE event dictionary with consistent formatting.

    Args:
        event_type: Event type (used as the SSE `event` field)
        data: Event payload (dict will be JSON-serialized, str used as-is)

    Returns:
        SSE event dict with 'event' and 'data' keys
    """
    if isinstance(data, dict):
        data = json.dumps(data)
    return {"event": event_type, "data": data}
