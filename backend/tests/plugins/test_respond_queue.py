"""Tests for draining the RESPOND queue (api.plugins.container.respond_queue)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace
from api.database.execution import db_create_execution
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.plugins.container.respond_queue import dispatch_next_queued_respond


def _make_org_repo(db, *, external_id: str) -> Repository:
    ws = db_create_workspace(db=db, name="test-ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    org = Organization(
        workspace_id=ws.id,
        name="acme",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider="github",
        settings={},
        installation_id="inst-1",
    )
    db.add(org)
    db.flush()
    repo = Repository(org_id=org.id, name="acme-app", external_id=external_id, provider="github")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def _seed_pr(db, repo: Repository, *, number: int = 3) -> PullRequest:
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=number,
        title="Add foo",
        author="alice",
        state="open",
        pr_url=f"https://github.com/{repo.name}/pull/{number}",
        head_branch="feat/foo",
        base_branch="main",
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr


@pytest.fixture
def mock_app(app):
    """Patch get_current_app for the respond_queue module only.

    Reuses the real (test) database plugin so DB assertions work normally,
    but swaps in a mock broker so publishes can be asserted on directly
    without a real Redis connection.
    """
    broker = AsyncMock()
    fake_faststream = AsyncMock()
    fake_faststream.get_broker = lambda: broker
    with patch("api.plugins.container.respond_queue.get_current_app") as gca:
        fake_app = AsyncMock()
        fake_app.database = app.database
        fake_app.faststream = fake_faststream
        gca.return_value = fake_app
        yield broker


@pytest.mark.asyncio
async def test_dispatches_oldest_queued_respond_behind_finished_one(app, db_session, mock_app):
    with app.database.session() as db:
        repo = _make_org_repo(db, external_id="9001")
        pr = _seed_pr(db, repo)
        finished = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.COMPLETED.value,
            retry_target_url=f"https://github.com/{repo.name}/pull/3#issuecomment-1",
        )
        queued = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.QUEUED.value,
            retry_target_url=f"https://github.com/{repo.name}/pull/3#issuecomment-2",
        )
        finished_id, queued_id = finished.id, queued.id

    await dispatch_next_queued_respond(finished_id)

    assert mock_app.publish.await_count == 1
    call = mock_app.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.github.manual_dispatch"
    dispatched = call.args[0]
    assert dispatched["execution_id"] == str(queued_id)
    assert dispatched["workflow"] == ExecutionWorkflow.RESPOND.value
    assert dispatched["pull_request_id"] == str(pr.id)


@pytest.mark.asyncio
async def test_noop_when_nothing_queued(app, db_session, mock_app):
    with app.database.session() as db:
        repo = _make_org_repo(db, external_id="9002")
        pr = _seed_pr(db, repo)
        execution = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.COMPLETED.value,
        )
        execution_id = execution.id

    await dispatch_next_queued_respond(execution_id)

    assert mock_app.publish.await_count == 0


@pytest.mark.asyncio
async def test_noop_for_non_respond_workflow(app, db_session, mock_app):
    """A finished REVIEW must never drain a queued RESPOND — unrelated workflows."""
    with app.database.session() as db:
        repo = _make_org_repo(db, external_id="9003")
        pr = _seed_pr(db, repo)
        db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.RESPOND.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.QUEUED.value,
        )
        review = db_create_execution(
            db,
            provider="github",
            pull_requests=[pr],
            workflow=ExecutionWorkflow.REVIEW.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.COMPLETED.value,
        )
        review_id = review.id

    await dispatch_next_queued_respond(review_id)

    assert mock_app.publish.await_count == 0
