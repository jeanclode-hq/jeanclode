"""Tests for GitHub pull_request webhook handler."""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models import Base
from api.models.organizations import Organization, Provider
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository
from api.models.workspaces import Workspace

WEBHOOK_SECRET = "test-webhook-secret"
PUBLISH_PATH = "api.routers.webhooks.github.pull_requests.publish_pull_request_event"


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def _make_pr_payload(
    action,
    repo_external_id="9999",
    repo_full_name="test-org/my-repo",
    pr_number=42,
    title="Fix the bug",
    author="dev-user",
    head_branch="jeanclode/fix-123",
    base_branch="main",
    head_sha="abc123",
    merged=False,
    state="open",
):
    return {
        "action": action,
        "pull_request": {
            "id": 77777,
            "number": pr_number,
            "title": title,
            "html_url": f"https://github.com/{repo_full_name}/pull/{pr_number}",
            "state": state,
            "merged": merged,
            "user": {"login": author},
            "head": {"ref": head_branch, "sha": head_sha},
            "base": {"ref": base_branch},
        },
        "repository": {
            "id": int(repo_external_id),
            "full_name": repo_full_name,
        },
        "sender": {"id": 100, "login": author},
    }


def _setup_repo(db, external_id="9999"):
    workspace = Workspace(name="test-workspace", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db.add(workspace)
    db.flush()

    org = Organization(
        workspace_id=workspace.id,
        name="test-org",
        external_org_id="org-123",
        provider=Provider.GITHUB.value,
    )
    db.add(org)
    db.flush()

    repo = Repository(
        org_id=org.id,
        external_id=external_id,
        name="my-repo",
        web_url="https://github.com/test-org/my-repo",
        provider=Provider.GITHUB.value,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    db.refresh(workspace)
    return repo, workspace


@pytest.fixture
def github_app(app):
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.github.config import GitHubAppConfig, GitHubPluginConfig
    from api.plugins.github.plugin import GitHubPlugin

    github_config = GitHubPluginConfig(
        enabled=True,
        app=GitHubAppConfig(
            client_id="test-client-id",
            client_secret="test-client-secret",
            app_id="12345",
            private_key_path="/tmp/fake-key.pem",
            webhook_secret=WEBHOOK_SECRET,
            name="test-app",
        ),
    )
    github_plugin = GitHubPlugin(github_config)
    app._plugins.append(github_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(github_plugin)


@pytest.fixture
def github_client(github_app):
    from fastapi.testclient import TestClient

    return TestClient(github_app.web.get_asgi_app())


# -- Route test: webhook queues to Redis --


def test_pr_event_queued(github_app, github_client):
    """pull_request event is published to Redis stream."""
    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock()

    with patch(
        "api.routers.webhooks.github.route.get_faststream_broker",
        return_value=mock_broker,
    ):
        payload = _make_pr_payload("opened")
        body = json.dumps(payload).encode()
        signature = _sign_payload(body, WEBHOOK_SECRET)

        response = github_client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    assert response.json()["processed"] is True
    mock_broker.publish.assert_called_once()
    call_kwargs = mock_broker.publish.call_args
    assert call_kwargs.kwargs.get("stream") == "jeanclode.events.github.pull_requests"


# -- Consumer tests: handler upserts PR and emits SSE --


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_creates_pr(mock_publish, github_app):
    """Handler creates PullRequest from event payload."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_pr_payload("opened", pr_number=10, title="Add feature")
    with github_app.database.session() as db:
        result = await handle_pull_request_event(event)

    assert result.processed is True

    with github_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr is not None
        assert pr.pr_number == 10
        assert pr.title == "Add feature"
        assert pr.state == PRState.OPEN.value
        assert pr.author == "dev-user"


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_merged_state(mock_publish, github_app):
    """Handler sets MERGED state for closed+merged PR."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_pr_payload("closed", merged=True, state="closed")
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    with github_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr.state == PRState.MERGED.value


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_closed_state(mock_publish, github_app):
    """Handler sets CLOSED state for closed without merge."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    event = _make_pr_payload("closed", merged=False, state="closed")
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    with github_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr.state == PRState.CLOSED.value


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_emits_sse(mock_publish, github_app):
    """Handler emits SSE event with correct workspace."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        _repo, workspace = _setup_repo(db)
        workspace_id = workspace.id

    event = _make_pr_payload("closed", merged=True, state="closed", pr_number=99)
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    mock_publish.assert_called_once()
    call_kwargs = mock_publish.call_args[1]
    assert call_kwargs["workspace_id"] == str(workspace_id)
    assert call_kwargs["payload"]["state"] == "merged"
    assert call_kwargs["payload"]["pr_number"] == 99


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_skips_sse_on_synchronize(mock_publish, github_app):
    """Handler skips SSE on synchronize (noise reduction)."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        _repo, _workspace = _setup_repo(db)

    event = _make_pr_payload("synchronize")
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    mock_publish.assert_not_called()


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_unknown_repo(mock_publish, github_app):
    """Handler returns not processed for untracked repos."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    event = _make_pr_payload("opened", repo_external_id="0000")
    result = await handle_pull_request_event(event)

    assert result.processed is False
    mock_publish.assert_not_called()


@patch(PUBLISH_PATH, new_callable=AsyncMock)
@pytest.mark.anyio
async def test_handler_author_immutable(mock_publish, github_app):
    """Author is set on creation, not overwritten on update."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with github_app.database.session() as db:
        repo, _workspace = _setup_repo(db)
        repo_id = repo.id

    # Create PR with author "dev-user"
    event = _make_pr_payload("opened", author="dev-user")
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    # Close PR with different actor "admin-user"
    event = _make_pr_payload("closed", author="admin-user", merged=True, state="closed")
    with github_app.database.session() as db:
        await handle_pull_request_event(event)

    with github_app.database.session() as db:
        pr = db.query(PullRequest).filter(PullRequest.repository_id == repo_id).first()
        assert pr.author == "dev-user"  # Still the original author
        assert pr.state == PRState.MERGED.value
