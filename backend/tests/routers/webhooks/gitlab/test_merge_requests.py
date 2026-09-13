"""Tests for GitLab merge_request webhook handler."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models import Base
from api.models.organizations import Organization, Provider
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository
from api.models.workspaces import Workspace

WEBHOOK_SECRET = "test-gitlab-webhook-secret"
PUBLISH_PATH = "api.routers.webhooks.gitlab.merge_requests.publish_pull_request_event"


def _make_mr_payload(
    action,
    repo_external_id="8888",
    mr_iid=10,
    title="Fix the bug",
    state="opened",
    source_branch="jeanclode/fix-123",
    target_branch="main",
    head_sha="def456",
    username="dev-user",
):
    return {
        "object_kind": "merge_request",
        "user": {"id": 42, "username": username, "name": "Dev User"},
        "object_attributes": {
            "id": 55555,
            "iid": mr_iid,
            "title": title,
            "state": state,
            "action": action,
            "url": f"https://gitlab.com/test-org/my-repo/-/merge_requests/{mr_iid}",
            "source_branch": source_branch,
            "target_branch": target_branch,
            "author_id": 42,
            "last_commit": {"id": head_sha},
        },
        "project": {
            "id": int(repo_external_id),
            "path_with_namespace": "test-org/my-repo",
        },
    }


def _setup_repo(db, external_id="8888"):
    workspace = Workspace(name="test-workspace", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db.add(workspace)
    db.flush()

    org = Organization(
        workspace_id=workspace.id,
        name="test-org",
        external_org_id="org-456",
        provider=Provider.GITLAB.value,
    )
    db.add(org)
    db.flush()

    repo = Repository(
        org_id=org.id,
        external_id=external_id,
        name="my-repo",
        web_url="https://gitlab.com/test-org/my-repo",
        provider=Provider.GITLAB.value,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    db.refresh(workspace)
    return repo, workspace


@pytest.fixture
def gitlab_app(app):
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.gitlab.config import GitLabPluginConfig
    from api.plugins.gitlab.plugin import GitLabPlugin

    gitlab_config = GitLabPluginConfig(
        enabled=True,
        webhook_secret=WEBHOOK_SECRET,
    )
    gitlab_plugin = GitLabPlugin(gitlab_config)
    app._plugins.append(gitlab_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(gitlab_plugin)


@pytest.fixture
def gitlab_client(gitlab_app):
    from fastapi.testclient import TestClient

    return TestClient(gitlab_app.web.get_asgi_app())


# -- Route tests --


def test_mr_event_queued(gitlab_app, gitlab_client):
    """merge_request event is published to Redis stream."""
    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock()

    with patch(
        "api.routers.webhooks.gitlab.route.get_faststream_broker",
        return_value=mock_broker,
    ):
        payload = _make_mr_payload("open")
        response = gitlab_client.post(
            "/webhooks/gitlab",
            content=json.dumps(payload).encode(),
            headers={"X-Gitlab-Token": WEBHOOK_SECRET, "Content-Type": "application/json"},
        )

    assert response.status_code == 202
    assert response.json()["processed"] is True
    mock_broker.publish.assert_called_once()
    call_kwargs = mock_broker.publish.call_args
    assert call_kwargs.kwargs.get("stream") == "jeanclode.events.gitlab.merge_requests"


def test_mr_invalid_token_rejected(gitlab_client):
    """Invalid X-Gitlab-Token returns 401."""
    payload = _make_mr_payload("open")
    response = gitlab_client.post(
        "/webhooks/gitlab",
        content=json.dumps(payload).encode(),
        headers={"X-Gitlab-Token": "wrong-secret", "Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_mr_non_merge_request_ignored(gitlab_app, gitlab_client):
    """Non-merge_request events are not processed."""
    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock()

    with patch(
        "api.routers.webhooks.gitlab.route.get_faststream_broker",
        return_value=mock_broker,
    ):
        payload = {"object_kind": "push", "ref": "refs/heads/main"}
        response = gitlab_client.post(
            "/webhooks/gitlab",
            content=json.dumps(payload).encode(),
            headers={"X-Gitlab-Token": WEBHOOK_SECRET, "Content-Type": "application/json"},
        )

    assert response.status_code == 202
    assert response.json()["processed"] is False
    mock_broker.publish.assert_not_called()


# -- Handler tests --


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_creates_pr_from_mr(mock_publish, gitlab_app):
    """Handler creates PullRequest from GitLab MR payload."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with gitlab_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_mr_payload("open", mr_iid=5, title="GitLab fix")
    with gitlab_app.database.session() as db:
        result = await handle_merge_request_event(event)

    assert result.processed is True

    with gitlab_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr is not None
        assert pr.pr_number == 5
        assert pr.title == "GitLab fix"
        assert pr.state == PRState.OPEN.value
        assert pr.author == "dev-user"


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_merged_state(mock_publish, gitlab_app):
    """Handler sets MERGED state for merged MR."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with gitlab_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_mr_payload("merge", state="merged")
    with gitlab_app.database.session() as db:
        await handle_merge_request_event(event)

    with gitlab_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr.state == PRState.MERGED.value


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_closed_state(mock_publish, gitlab_app):
    """Handler sets CLOSED state for closed MR."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with gitlab_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_mr_payload("close", state="closed")
    with gitlab_app.database.session() as db:
        await handle_merge_request_event(event)

    with gitlab_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr.state == PRState.CLOSED.value


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_emits_sse(mock_publish, gitlab_app):
    """Handler emits SSE event with correct workspace."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with gitlab_app.database.session() as db:
        _repo, workspace = _setup_repo(db)
        workspace_id = workspace.id

    event = _make_mr_payload("merge", state="merged", mr_iid=7)
    with gitlab_app.database.session() as db:
        await handle_merge_request_event(event)

    mock_publish.assert_called_once()
    call_kwargs = mock_publish.call_args[1]
    assert call_kwargs["workspace_id"] == str(workspace_id)
    assert call_kwargs["payload"]["state"] == "merged"
    assert call_kwargs["payload"]["pr_number"] == 7
