"""Tests for SSE resource manager."""

import asyncio

import pytest

from api.sse.manager import SHUTDOWN_SENTINEL, SSEResourceManager


@pytest.fixture
def manager():
    return SSEResourceManager()


def test_register_client(manager):
    """register_client returns a queue and tracks it."""
    queue = manager.register_client("organization", "org-1")
    assert isinstance(queue, asyncio.Queue)
    assert manager.get_client_count() == 1


def test_register_multiple_clients(manager):
    """Multiple clients can register for the same resource."""
    q1 = manager.register_client("organization", "org-1")
    q2 = manager.register_client("organization", "org-1")
    assert q1 is not q2
    assert manager.get_client_count("organization", "org-1") == 2


def test_unregister_client(manager):
    """unregister_client removes the queue and cleans up empty dicts."""
    queue = manager.register_client("organization", "org-1")
    manager.unregister_client("organization", "org-1", queue)

    assert manager.get_client_count() == 0
    # Internal dicts should be cleaned up
    assert "organization" not in manager._resources


def test_unregister_unknown_resource_type(manager):
    """unregister_client handles unknown resource type gracefully."""
    queue = manager.register_client("organization", "org-1")
    manager.unregister_client("unknown", "org-1", queue)
    # Original registration still intact
    assert manager.get_client_count() == 1


def test_unregister_unknown_resource_id(manager):
    """unregister_client handles unknown resource ID gracefully."""
    queue = manager.register_client("organization", "org-1")
    manager.unregister_client("organization", "unknown", queue)
    assert manager.get_client_count() == 1


def test_get_queues(manager):
    """get_queues returns all queues for a resource."""
    q1 = manager.register_client("organization", "org-1")
    q2 = manager.register_client("organization", "org-1")
    manager.register_client("organization", "org-2")

    queues = manager.get_queues("organization", "org-1")
    assert len(queues) == 2
    assert q1 in queues
    assert q2 in queues


def test_get_queues_empty(manager):
    """get_queues returns empty list for unknown resource."""
    assert manager.get_queues("organization", "org-1") == []
    assert manager.get_queues("unknown", "id") == []


def test_get_client_count_filters(manager):
    """get_client_count supports filtering by type and id."""
    manager.register_client("organization", "org-1")
    manager.register_client("organization", "org-1")
    manager.register_client("organization", "org-2")
    manager.register_client("issue", "issue-1")

    assert manager.get_client_count() == 4
    assert manager.get_client_count("organization") == 3
    assert manager.get_client_count("organization", "org-1") == 2
    assert manager.get_client_count("issue") == 1
    assert manager.get_client_count("unknown") == 0


def test_clear_all_clients(manager):
    """clear_all_clients removes everything."""
    manager.register_client("organization", "org-1")
    manager.register_client("issue", "issue-1")
    manager.clear_all_clients()
    assert manager.get_client_count() == 0


async def test_broadcast_to_resource(manager):
    """broadcast_to_resource fans out to all registered queues."""
    q1 = manager.register_client("organization", "org-1")
    q2 = manager.register_client("organization", "org-1")
    q3 = manager.register_client("organization", "org-2")

    await manager.broadcast_to_resource("organization", "org-1", {"action": "test"})

    assert not q1.empty()
    assert not q2.empty()
    assert q3.empty()

    assert await q1.get() == {"action": "test"}
    assert await q2.get() == {"action": "test"}


async def test_broadcast_no_clients(manager):
    """broadcast_to_resource is a no-op when no clients registered."""
    await manager.broadcast_to_resource("organization", "org-1", {"action": "test"})


async def test_shutdown_all(manager):
    """shutdown_all sends sentinel to all queues."""
    q1 = manager.register_client("organization", "org-1")
    q2 = manager.register_client("issue", "issue-1")

    await manager.shutdown_all()

    assert await q1.get() == SHUTDOWN_SENTINEL
    assert await q2.get() == SHUTDOWN_SENTINEL


async def test_shutdown_all_no_clients(manager):
    """shutdown_all is a no-op when no clients registered."""
    await manager.shutdown_all()


def test_cleanup_partial_unregister(manager):
    """Unregistering one client from a multi-client resource keeps the rest."""
    q1 = manager.register_client("organization", "org-1")
    q2 = manager.register_client("organization", "org-1")

    manager.unregister_client("organization", "org-1", q1)

    assert manager.get_client_count("organization", "org-1") == 1
    assert manager.get_queues("organization", "org-1") == [q2]
