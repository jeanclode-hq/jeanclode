"""Tests for `POST /executions/{execution_id}/cancel`.

Covers the SCHEDULED path (ADR-010, DB-only), the RUNNING path (which also
has to kill the actual container/Job), and that QUEUED is rejected — it has
no container yet and cancelling it would race its own dispatch.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace, db_create_workspace_membership
from api.database.execution import db_create_execution
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository


def _make_issue_graph(db, mock_auth, *, provider="sentry"):
    ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

    org = Organization(
        workspace_id=ws.id,
        name="test-org",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
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

    issue = Issue(
        repository_id=repo.id,
        external_id=f"issue-{uuid.uuid4().hex[:6]}",
        title="Boom",
        level="error",
        triage_result=TriageResult.ACTIONABLE.value,
    )
    db.add(issue)
    db.commit()
    for obj in [ws, org, repo, issue]:
        db.refresh(obj)
    return ws, org, repo, issue


@pytest.fixture
def mock_publish_events():
    """Stub the SSE publishers so they don't try to push to FastStream."""
    with (
        patch("api.routers.executions.route.publish_execution_event", new=AsyncMock()) as exec_evt,
        patch("api.routers.executions.route.publish_pull_request_event", new=AsyncMock()) as pr_evt,
    ):
        yield exec_evt, pr_evt


def test_cancel_scheduled_execution(auth_client, app, mock_auth, mock_publish_events):
    """A SCHEDULED execution is cancelled via a plain DB update, no container involved."""
    with app.database.session() as db:
        _, org, _, issue = _make_issue_graph(db, mock_auth)
        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.SCHEDULED.value,
        )
        execution_id = str(execution.id)

    with patch("api.routers.executions.route._resolve_container_backend") as mock_resolve_backend:
        resp = auth_client.post(f"/executions/{execution_id}/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"execution_id": execution_id, "cancelled": True}
    mock_resolve_backend.assert_not_called()

    with app.database.session() as db:
        from api.models.executions import Execution

        refreshed = db.query(Execution).filter(Execution.id == uuid.UUID(execution_id)).first()
        assert refreshed.status == ExecutionStatus.CANCELLED.value


def test_cancel_running_execution_stops_container(auth_client, app, mock_auth, mock_publish_events):
    """A RUNNING execution's container is killed and unregistered, then marked CANCELLED."""
    with app.database.session() as db:
        _, org, _, issue = _make_issue_graph(db, mock_auth)
        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.RUNNING.value,
        )
        execution.container_id = "jc-sentry-abc123"
        db.commit()
        execution_id = str(execution.id)

    mock_backend = AsyncMock()
    with patch(
        "api.routers.executions.route._resolve_container_backend", return_value=mock_backend
    ):
        resp = auth_client.post(f"/executions/{execution_id}/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"execution_id": execution_id, "cancelled": True}
    mock_backend.unregister_execution.assert_awaited_once_with(execution_id)
    mock_backend.stop_container.assert_awaited_once_with("jc-sentry-abc123")

    with app.database.session() as db:
        from api.models.executions import Execution

        refreshed = db.query(Execution).filter(Execution.id == uuid.UUID(execution_id)).first()
        assert refreshed.status == ExecutionStatus.CANCELLED.value


def test_cancel_batch_publishes_one_event(auth_client, app, mock_auth, mock_publish_events):
    """Every SSE event makes every open dashboard refetch, so a batch cancels with one."""
    exec_evt, pr_evt = mock_publish_events
    with app.database.session() as db:
        _, org, repo, issue = _make_issue_graph(db, mock_auth)
        second = Issue(
            repository_id=repo.id,
            external_id=f"issue-{uuid.uuid4().hex[:6]}",
            title="Boom again",
            level="error",
        )
        db.add(second)
        db.flush()
        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue, second],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.SCHEDULED.value,
        )
        execution_id = str(execution.id)
        issue_ids = {str(issue.id), str(second.id)}

    resp = auth_client.post(f"/executions/{execution_id}/cancel")

    assert resp.status_code == 200
    exec_evt.assert_awaited_once()
    pr_evt.assert_not_awaited()
    payload = exec_evt.await_args.kwargs["payload"]
    assert set(payload["issue_ids"]) == issue_ids
    assert payload["status"] == ExecutionStatus.CANCELLED.value


def test_cancel_queued_execution_returns_409(auth_client, app, mock_auth, mock_publish_events):
    """A QUEUED execution can't be cancelled — it has no container yet, and
    cancelling it would race the consumer that's about to dispatch it."""
    with app.database.session() as db:
        _, org, _, issue = _make_issue_graph(db, mock_auth)
        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.QUEUED.value,
        )
        execution_id = str(execution.id)

    with patch("api.routers.executions.route._resolve_container_backend") as mock_resolve_backend:
        resp = auth_client.post(f"/executions/{execution_id}/cancel")

    assert resp.status_code == 409
    mock_resolve_backend.assert_not_called()

    with app.database.session() as db:
        from api.models.executions import Execution

        refreshed = db.query(Execution).filter(Execution.id == uuid.UUID(execution_id)).first()
        assert refreshed.status == ExecutionStatus.QUEUED.value


def test_cancel_terminal_execution_returns_409(auth_client, app, mock_auth, mock_publish_events):
    """A COMPLETED execution can't be cancelled."""
    with app.database.session() as db:
        _, org, _, issue = _make_issue_graph(db, mock_auth)
        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.COMPLETED.value,
        )
        execution_id = str(execution.id)

    resp = auth_client.post(f"/executions/{execution_id}/cancel")
    assert resp.status_code == 409


def test_cancel_unknown_execution_returns_404(auth_client, app, mock_auth, mock_publish_events):
    resp = auth_client.post(f"/executions/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


def test_cancel_running_respond_dispatches_queued_respond_behind_it(
    auth_client, app, mock_auth, mock_publish_events
):
    """Cancelling a RUNNING respond immediately publishes the one queued behind it.

    This is the case the status-stream hook can't see on its own (cancel
    deliberately bypasses that stream) — cancel_execution has to trigger the
    queue drain itself.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)
        org = Organization(
            workspace_id=ws.id,
            name="test-org",
            external_org_id=f"org-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(org)
        db.flush()
        repo = Repository(org_id=org.id, name="acme-app", external_id="4001", provider="github")
        db.add(repo)
        db.flush()
        pr = PullRequest(
            repository_id=repo.id,
            pr_number=5,
            title="Add foo",
            author="alice",
            state="open",
            pr_url="https://github.com/acme/acme-app/pull/5",
            head_branch="feat/foo",
            base_branch="main",
        )
        db.add(pr)
        db.commit()
        db.refresh(pr)

        running = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.RUNNING.value,
            retry_target_url="https://github.com/acme/acme-app/pull/5#issuecomment-1",
        )
        running.container_id = "jc-github-abc123"
        db.commit()
        running_id = str(running.id)

        queued = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.QUEUED.value,
            retry_target_url="https://github.com/acme/acme-app/pull/5#issuecomment-2",
        )
        queued_id = str(queued.id)

    mock_backend = AsyncMock()
    mock_broker = AsyncMock()
    fake_faststream = AsyncMock()
    fake_faststream.get_broker = lambda: mock_broker
    with (
        patch("api.routers.executions.route._resolve_container_backend", return_value=mock_backend),
        patch("api.plugins.container.respond_queue.get_current_app") as gca,
    ):
        fake_app = AsyncMock()
        fake_app.database = app.database
        fake_app.faststream = fake_faststream
        gca.return_value = fake_app

        resp = auth_client.post(f"/executions/{running_id}/cancel")

    assert resp.status_code == 200

    with app.database.session() as db:
        from api.models.executions import Execution

        refreshed_running = (
            db.query(Execution).filter(Execution.id == uuid.UUID(running_id)).first()
        )
        assert refreshed_running.status == ExecutionStatus.CANCELLED.value
        # The queued one is still QUEUED in the DB — dispatch is what starts
        # its container, not a status write.
        refreshed_queued = db.query(Execution).filter(Execution.id == uuid.UUID(queued_id)).first()
        assert refreshed_queued.status == ExecutionStatus.QUEUED.value

    mock_broker.publish.assert_awaited_once()
    call = mock_broker.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.github.manual_dispatch"
    assert call.args[0]["execution_id"] == queued_id
