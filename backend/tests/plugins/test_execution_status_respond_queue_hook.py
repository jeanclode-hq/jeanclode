"""Wiring test: RESPOND terminal statuses drain the respond queue.

Exercises the actual github/gitlab status-consumer handlers (not
``dispatch_next_queued_respond`` in isolation, already covered by
``tests/plugins/test_respond_queue.py``) to prove the hook fires on the
right statuses/workflow and not otherwise.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace
from api.database.execution import db_create_execution
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository


def _make_pr_execution(db, *, provider: str, external_id: str, workflow: str, status: str):
    ws = db_create_workspace(db=db, name="test-ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    org = Organization(
        workspace_id=ws.id,
        name="acme",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider=provider,
        settings={},
    )
    db.add(org)
    db.flush()
    repo = Repository(org_id=org.id, name="acme-app", external_id=external_id, provider=provider)
    db.add(repo)
    db.flush()
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=1,
        title="Add foo",
        author="alice",
        state="open",
        pr_url=f"https://example.com/{repo.name}/pull/1",
        head_branch="feat/foo",
        base_branch="main",
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    execution = db_create_execution(
        db, provider=provider, pull_requests=[pr], workflow=workflow, status=status
    )
    return execution


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_github_terminal_respond_drains_queue(app, db_session, terminal_status):
    from api.plugins.github.consumer import ExecutionStatusMessage, _handle_execution_status

    with app.database.session() as db:
        execution = _make_pr_execution(
            db,
            provider="github",
            external_id="5001",
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.RUNNING.value,
        )
        execution_id = execution.id

    with (
        patch("api.plugins.github.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch(
            "api.plugins.github.consumer.dispatch_next_queued_respond", new=AsyncMock()
        ) as dispatch_next,
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(execution_id), status=terminal_status)
        )

    dispatch_next.assert_awaited_once_with(execution_id)


@pytest.mark.asyncio
async def test_github_running_respond_does_not_drain_queue(app, db_session):
    """A mid-run status ("running") must not be treated as terminal."""
    from api.plugins.github.consumer import ExecutionStatusMessage, _handle_execution_status

    with app.database.session() as db:
        execution = _make_pr_execution(
            db,
            provider="github",
            external_id="5002",
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.QUEUED.value,
        )
        execution_id = execution.id

    with (
        patch("api.plugins.github.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch(
            "api.plugins.github.consumer.dispatch_next_queued_respond", new=AsyncMock()
        ) as dispatch_next,
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(execution_id), status="running")
        )

    dispatch_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_terminal_review_does_not_drain_respond_queue(app, db_session):
    """A finished REVIEW is a different workflow — must never touch the respond queue."""
    from api.plugins.github.consumer import ExecutionStatusMessage, _handle_execution_status

    with app.database.session() as db:
        execution = _make_pr_execution(
            db,
            provider="github",
            external_id="5003",
            workflow=ExecutionWorkflow.REVIEW.value,
            status=ExecutionStatus.RUNNING.value,
        )
        execution_id = execution.id

    with (
        patch("api.plugins.github.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch(
            "api.plugins.github.consumer.dispatch_next_queued_respond", new=AsyncMock()
        ) as dispatch_next,
        patch("api.plugins.github.consumer._resolve_execution_targets", return_value=("", [])),
        patch("api.plugins.github.consumer.publish_pull_request_event", new=AsyncMock()),
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(execution_id), status="completed")
        )

    dispatch_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_gitlab_terminal_respond_drains_queue(app, db_session):
    from api.plugins.gitlab.consumer import ExecutionStatusMessage, _handle_execution_status

    with app.database.session() as db:
        execution = _make_pr_execution(
            db,
            provider="gitlab",
            external_id="5004",
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.RUNNING.value,
        )
        execution_id = execution.id

    with (
        patch("api.plugins.gitlab.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch(
            "api.plugins.gitlab.consumer.dispatch_next_queued_respond", new=AsyncMock()
        ) as dispatch_next,
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(execution_id), status="completed")
        )

    dispatch_next.assert_awaited_once_with(execution_id)
