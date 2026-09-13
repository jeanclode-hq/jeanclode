"""Tests for SSE fan-out consumer."""

import pytest

from api.routers.stream.consumer import handle_sse_event
from api.routers.stream.schemas import SSEEventMessage
from api.sse import sse_manager
from api.sse.schemas import EventType

WORKSPACE_ID = "workspace-123"


@pytest.fixture(autouse=True)
def _clear_sse():
    """Clear SSE clients before and after each test."""
    sse_manager.clear_all_clients()
    yield
    sse_manager.clear_all_clients()


def _make_message(**overrides) -> SSEEventMessage:
    defaults = {
        "event_type": EventType.ISSUE,
        "action": "created",
        "workspace_id": WORKSPACE_ID,
        "payload": {"issue_id": "issue-abc", "status": "pending", "title": "Test"},
    }
    defaults.update(overrides)
    return SSEEventMessage(**defaults)


@pytest.mark.asyncio
async def test_fanout_to_workspace_clients():
    """Events are fanned out to all clients subscribed to the workspace."""
    q1 = sse_manager.register_client(WORKSPACE_ID, "client-1")
    q2 = sse_manager.register_client(WORKSPACE_ID, "client-2")

    message = _make_message()
    await handle_sse_event(message)

    event1 = q1.get_nowait()
    event2 = q2.get_nowait()
    assert event1["action"] == "created"
    assert event2["action"] == "created"
    assert event1["workspace_id"] == WORKSPACE_ID


@pytest.mark.asyncio
async def test_fanout_no_clients():
    """No error when no clients are connected."""
    message = _make_message()
    await handle_sse_event(message)  # should not raise


@pytest.mark.asyncio
async def test_fanout_only_to_matching_workspace():
    """Only clients registered for the event's workspace get events."""
    matching_queue = sse_manager.register_client(WORKSPACE_ID, "client-1")
    other_queue = sse_manager.register_client("other-workspace", "client-2")

    message = _make_message()
    await handle_sse_event(message)

    assert not matching_queue.empty()
    assert other_queue.empty()
