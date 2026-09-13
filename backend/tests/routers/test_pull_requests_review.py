"""Tests for the manual PR workflow trigger endpoint.

`POST /pull-requests/{pr_id}/{workflow}` queues a REVIEW or SUMMARY
execution and publishes a ManualDispatchEvent on the provider's stream.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace, db_create_workspace_membership
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.organizations import Organization
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository


def _make_pr_graph(db, mock_auth, *, provider="github", author="testauth"):
    ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

    org = Organization(
        workspace_id=ws.id,
        name="test-git-org",
        external_org_id=f"git-{uuid.uuid4().hex[:6]}",
        provider=provider,
    )
    db.add(org)
    db.flush()

    repo = Repository(
        org_id=org.id,
        name="my-repo",
        external_id=f"repo-{uuid.uuid4().hex[:6]}",
        provider=provider,
    )
    db.add(repo)
    db.flush()

    pr = PullRequest(
        repository_id=repo.id,
        pr_number=42,
        title="Add foo",
        author=author,
        state=PRState.OPEN.value,
        pr_url=f"https://example/{uuid.uuid4().hex[:6]}/pull/42",
        head_branch="feat/foo",
        base_branch="main",
    )
    db.add(pr)
    db.commit()
    for obj in [ws, org, repo, pr]:
        db.refresh(obj)
    return ws, org, repo, pr


@pytest.fixture
def mock_broker():
    """Patch get_faststream_broker so broker.publish doesn't hit Redis."""
    broker = AsyncMock()
    with patch("api.routers.pull_requests.route.get_faststream_broker", return_value=broker):
        yield broker


@pytest.fixture
def mock_publish_pr_event():
    """Stub the SSE publisher so it doesn't try to push to FastStream."""
    with patch(
        "api.routers.pull_requests.route.publish_pull_request_event", new=AsyncMock()
    ) as mock:
        yield mock


def test_review_returns_403_when_not_author(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """Only the PR author can manually trigger a review."""
    with app.database.session() as db:
        _, _, _, pr = _make_pr_graph(db, mock_auth, author="someone-else")
        pr_id = str(pr.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/review")
    assert resp.status_code == 403
    mock_broker.publish.assert_not_awaited()


def test_review_returns_403_when_not_workspace_member(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """Non-members can't trigger a review even if they happen to be the PR author."""
    with app.database.session() as db:
        # Build the PR graph in a workspace the user is NOT a member of.
        # Match the user's auth username to ``pr.author`` so the author
        # gate would otherwise pass — only the workspace gate should reject.
        ws = db_create_workspace(db=db, name="other-ws", slug=f"other-{uuid.uuid4().hex[:6]}")
        org = Organization(
            workspace_id=ws.id,
            name="other-git-org",
            external_org_id=f"git-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(org)
        db.flush()
        repo = Repository(
            org_id=org.id,
            name="r",
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(repo)
        db.flush()
        pr = PullRequest(
            repository_id=repo.id,
            pr_number=1,
            title="t",
            author="testauth",
            state=PRState.OPEN.value,
            pr_url=f"https://example/{uuid.uuid4().hex[:6]}/pull/1",
            head_branch="x",
            base_branch="main",
        )
        db.add(pr)
        db.commit()
        pr_id = str(pr.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/review")
    assert resp.status_code == 403
    mock_broker.publish.assert_not_awaited()


def test_review_returns_404_when_pr_missing(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """A non-existent PR id returns 404 cleanly."""
    resp = auth_client.post(f"/pull-requests/{uuid.uuid4()}/review")
    assert resp.status_code == 404


def test_review_returns_409_when_active_execution(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """A PR with a queued or running review can't be re-triggered."""
    with app.database.session() as db:
        _, _, _, pr = _make_pr_graph(db, mock_auth, author="testauth")
        running = Execution(
            provider="github",
            workflow=ExecutionWorkflow.REVIEW.value,
            status=ExecutionStatus.RUNNING.value,
        )
        running.pull_requests = [pr]
        db.add(running)
        db.commit()
        pr_id = str(pr.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/review")
    assert resp.status_code == 409
    mock_broker.publish.assert_not_awaited()


@pytest.mark.parametrize("workflow", ["fix", "summary", "garbage"])
def test_manual_trigger_400_for_non_review_workflow(
    workflow, auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """Only REVIEW is manually triggerable. SUMMARY/FIX are auto-only."""
    with app.database.session() as db:
        _, _, _, pr = _make_pr_graph(db, mock_auth, author="testauth")
        pr_id = str(pr.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/{workflow}")
    assert resp.status_code == 400
    mock_broker.publish.assert_not_awaited()


def test_manual_review_publishes_to_provider_stream(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """Successful REVIEW trigger creates a QUEUED execution and publishes."""
    with app.database.session() as db:
        _, org, _, pr = _make_pr_graph(db, mock_auth, provider="github", author="testauth")
        pr_id = str(pr.id)
        org_id = str(org.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/review")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == ExecutionStatus.QUEUED.value
    execution_id = body["execution_id"]

    mock_broker.publish.assert_awaited_once()
    call = mock_broker.publish.await_args
    payload = call.args[0] if call.args else call.kwargs["message"]
    assert payload["pull_request_id"] == pr_id
    assert payload["execution_id"] == execution_id
    assert payload["organization_id"] == org_id
    assert payload["workflow"] == "review"
    assert call.kwargs["stream"] == "jeanclode.events.github.manual_dispatch"

    with app.database.session() as db:
        execution = db.query(Execution).filter(Execution.id == uuid.UUID(execution_id)).one()
        assert execution.workflow == "review"
        assert execution.trigger == "manual"
        assert execution.status == ExecutionStatus.QUEUED.value
        assert [pr.id for pr in execution.pull_requests] == [uuid.UUID(pr_id)]


def test_gitlab_provider_publishes_to_gitlab_stream(
    auth_client, app, mock_auth, mock_broker, mock_publish_pr_event
):
    """GitLab MRs route to the gitlab-prefixed stream and check gitlab identity."""
    # Create a gitlab identity for the user so the author check passes.
    from api.models.identities import ProviderIdentity

    with app.database.session() as db:
        gitlab_identity = ProviderIdentity(
            provider="gitlab",
            external_id="gl-1",
            username="testauthgl",
            user_id=mock_auth.id,
        )
        db.add(gitlab_identity)
        db.commit()
        _, _, _, pr = _make_pr_graph(db, mock_auth, provider="gitlab", author="testauthgl")
        pr_id = str(pr.id)

    resp = auth_client.post(f"/pull-requests/{pr_id}/review")
    assert resp.status_code == 200, resp.json()
    call = mock_broker.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.gitlab.manual_dispatch"
