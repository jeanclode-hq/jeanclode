"""Tests for GitLab webhook dispatch.

GitLab sends two payload shapes down one URL: ``object_kind`` for project and
group activity, ``event_name`` (and no ``object_kind``) for the lifecycle
events — project, subgroup, member, and everything a system hook reports.
Dispatching on ``object_kind`` alone drops every event of the second kind on
the floor, which is what these cases pin down.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models import Base

WEBHOOK_SECRET = "test-gitlab-webhook-secret"
BROKER_PATH = "api.routers.webhooks.gitlab.route.get_faststream_broker"


@pytest.fixture
def gitlab_app(app):
    """App fixture with a GitLab plugin configured for webhook verification."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.gitlab.config import GitLabPluginConfig
    from api.plugins.gitlab.plugin import GitLabPlugin

    gitlab_plugin = GitLabPlugin(
        GitLabPluginConfig(
            enabled=True,
            instance_url="https://gitlab.example.com",
            webhook_secret=WEBHOOK_SECRET,
        )
    )
    gitlab_plugin._http = AsyncMock()
    app._plugins.append(gitlab_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(gitlab_plugin)


@pytest.fixture
def gitlab_client(gitlab_app):
    from fastapi.testclient import TestClient

    return TestClient(gitlab_app.web.get_asgi_app())


def _post(client, payload: dict, hook: str | None = None):
    """Send a webhook the way GitLab does, with its hook-type header."""
    headers = {
        "X-Gitlab-Token": WEBHOOK_SECRET,
        "X-Gitlab-Instance": "https://gitlab.example.com",
        "Content-Type": "application/json",
    }
    if hook:
        headers["X-Gitlab-Event"] = hook

    broker = MagicMock()
    broker.publish = AsyncMock()
    with patch(BROKER_PATH, return_value=broker):
        response = client.post(
            "/webhooks/gitlab", content=json.dumps(payload).encode(), headers=headers
        )
    return response, broker


def _stream(broker) -> str:
    return broker.publish.call_args.kwargs["stream"]


def test_group_project_event_is_queued_to_the_group_stream(gitlab_client):
    """``Project Hook`` — a project created in a connected group."""
    response, broker = _post(
        gitlab_client,
        {
            "event_name": "project_create",
            "project_id": 74,
            "project_namespace_id": 12,
            "path_with_namespace": "acme/storecloud",
        },
        hook="Project Hook",
    )

    assert response.status_code == 202
    assert response.json()["processed"] is True
    assert _stream(broker) == "jeanclode.events.gitlab.group"
    assert broker.publish.call_args.args[0]["instance_url"] == "https://gitlab.example.com"
    assert broker.publish.call_args.args[0]["payload"]["project_id"] == 74


def test_system_hook_project_event_is_queued_to_the_system_stream(gitlab_client):
    """The same payload from ``System Hook`` reaches the instance-wide handler."""
    response, broker = _post(
        gitlab_client,
        {
            "event_name": "project_create",
            "project_id": 74,
            "project_namespace_id": 12,
            "path_with_namespace": "acme/storecloud",
        },
        hook="System Hook",
    )

    assert response.status_code == 202
    assert _stream(broker) == "jeanclode.events.gitlab.system"


def test_member_event_reaches_the_member_consumer(gitlab_client):
    """``Member Hook`` payloads carry ``event_name`` and no ``object_kind``.

    Dispatching on ``object_kind`` alone meant every group membership change
    was answered with "not supported" and never reached ``member_consumer``.
    """
    response, broker = _post(
        gitlab_client,
        {
            "event_name": "user_add_to_group",
            "group_id": 100,
            "group_access": "Developer",
            "user_id": 64,
            "user_username": "test_user",
        },
        hook="Member Hook",
    )

    assert response.status_code == 202
    assert response.json()["processed"] is True
    assert _stream(broker) == "jeanclode.events.gitlab.members"
    # The consumer reads the payload flat, not wrapped in an envelope.
    assert broker.publish.call_args.args[0]["event_name"] == "user_add_to_group"


def test_system_hook_member_event_reaches_the_member_consumer(gitlab_client):
    """A system hook reports the same membership changes, in the same shape."""
    response, broker = _post(
        gitlab_client,
        {"event_name": "user_remove_from_group", "group_id": 100, "user_id": 64},
        hook="System Hook",
    )

    assert response.status_code == 202
    assert _stream(broker) == "jeanclode.events.gitlab.members"


def test_subgroup_event_is_queued_to_the_group_stream(gitlab_client):
    """``Subgroup Hook`` — a subgroup removed under a connected group."""
    response, broker = _post(
        gitlab_client,
        {
            "event_name": "subgroup_destroy",
            "group_id": 600,
            "full_path": "acme/platform",
            "parent_group_id": 500,
        },
        hook="Subgroup Hook",
    )

    assert response.status_code == 202
    assert _stream(broker) == "jeanclode.events.gitlab.group"


def test_merge_request_still_dispatches_on_object_kind(gitlab_client):
    """The ``object_kind`` families are untouched by the lifecycle branch."""
    response, broker = _post(
        gitlab_client,
        {
            "object_kind": "merge_request",
            "object_attributes": {"iid": 1, "action": "open"},
            "project": {"id": 8888},
        },
        hook="Merge Request Hook",
    )

    assert response.status_code == 202
    assert _stream(broker) == "jeanclode.events.gitlab.merge_requests"


def test_system_hook_merge_request_keeps_its_object_kind_route(gitlab_client):
    """System hooks send MRs with ``object_kind`` — those are ordinary MR events."""
    response, broker = _post(
        gitlab_client,
        {
            "object_kind": "merge_request",
            "event_type": "merge_request",
            "object_attributes": {"iid": 1, "action": "open"},
            "project": {"id": 8888},
        },
        hook="System Hook",
    )

    assert response.status_code == 202
    assert _stream(broker) == "jeanclode.events.gitlab.merge_requests"


def test_unrelated_system_hook_event_is_ignored(gitlab_client):
    """A system hook also reports users, keys and pushes — none of them ours."""
    response, broker = _post(
        gitlab_client,
        {"event_name": "key_create", "id": 7, "username": "someone"},
        hook="System Hook",
    )

    assert response.status_code == 202
    assert response.json()["processed"] is False
    broker.publish.assert_not_called()


def test_group_hook_rejects_a_system_only_event(gitlab_client):
    """``group_destroy`` has no group-webhook equivalent; only system hooks send it."""
    response, broker = _post(
        gitlab_client,
        {"event_name": "group_destroy", "group_id": 500, "full_path": "acme"},
        hook="Subgroup Hook",
    )

    assert response.status_code == 202
    assert response.json()["processed"] is False
    broker.publish.assert_not_called()


def test_invalid_token_is_rejected(gitlab_client):
    """Verification still runs before any dispatch."""
    response = gitlab_client.post(
        "/webhooks/gitlab",
        content=json.dumps({"event_name": "project_create", "project_id": 1}).encode(),
        headers={"X-Gitlab-Token": "wrong-secret", "Content-Type": "application/json"},
    )

    assert response.status_code == 401
