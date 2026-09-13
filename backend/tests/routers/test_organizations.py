"""Tests for SSE stream endpoint."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from api.routers.stream.route import stream_events
from api.sse import EventType, sse_manager

WORKSPACE_ID = "ws-test-123"


def _make_user():
    """Create a minimal user-like object for tests."""
    return SimpleNamespace(id=uuid4())


async def test_stream_yields_connected_event():
    """SSE stream yields a connected event as first message."""
    sse_manager.clear_all_clients()

    user = _make_user()
    response = await stream_events(workspace_id=WORKSPACE_ID, current_user=user)
    gen = response.body_iterator

    events = []
    async for chunk in gen:
        events.append(chunk)
        if chunk.get("event") == EventType.CONNECTED:
            break

    assert len(events) >= 1
    connected = events[-1]
    assert connected["event"] == EventType.CONNECTED
    data = json.loads(connected["data"])
    assert "client_id" in data
    assert data["workspace_id"] == WORKSPACE_ID

    sse_manager.clear_all_clients()


async def test_stream_receives_broadcast_with_resource_event_type():
    """Events use resource type as SSE event field, action in payload."""
    sse_manager.clear_all_clients()

    user = _make_user()
    response = await stream_events(workspace_id=WORKSPACE_ID, current_user=user)
    gen = response.body_iterator

    collected: list[dict[str, str]] = []

    async def read() -> None:
        async for chunk in gen:
            collected.append(chunk)
            if chunk.get("event") == EventType.ISSUE:
                return

    task = asyncio.create_task(read())

    for _ in range(50):
        await asyncio.sleep(0.05)
        if sse_manager.get_client_count(WORKSPACE_ID) > 0:
            break

    assert sse_manager.get_client_count(WORKSPACE_ID) > 0
    await asyncio.sleep(0.1)

    client_id = None
    for event in collected:
        if event.get("event") == EventType.CONNECTED:
            data = json.loads(event["data"])
            client_id = data["client_id"]
            break
    assert client_id is not None

    await sse_manager.broadcast_to_resource(
        WORKSPACE_ID,
        client_id,
        {
            "event_type": EventType.ISSUE,
            "action": "created",
            "workspace_id": WORKSPACE_ID,
            "issue": {"id": "issue-1"},
        },
    )

    await asyncio.wait_for(task, timeout=5.0)

    issue_events = [e for e in collected if e.get("event") == EventType.ISSUE]
    assert len(issue_events) == 1
    data = json.loads(issue_events[0]["data"])
    assert data["workspace_id"] == WORKSPACE_ID
    assert data["action"] == "created"
    assert "event_type" not in data

    sse_manager.clear_all_clients()


async def test_stream_shutdown_sentinel():
    """Shutdown sentinel causes the SSE stream to end."""
    sse_manager.clear_all_clients()

    user = _make_user()
    response = await stream_events(workspace_id=WORKSPACE_ID, current_user=user)
    gen = response.body_iterator

    collected: list[dict[str, str]] = []

    async def read() -> None:
        async for chunk in gen:
            collected.append(chunk)

    task = asyncio.create_task(read())

    for _ in range(50):
        await asyncio.sleep(0.05)
        if sse_manager.get_client_count(WORKSPACE_ID) > 0:
            break

    await sse_manager.shutdown_all()
    await asyncio.wait_for(task, timeout=5.0)

    assert task.done()

    sse_manager.clear_all_clients()
