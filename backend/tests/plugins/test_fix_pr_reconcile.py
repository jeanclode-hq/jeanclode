"""Part E1 — active reconciliation of open fix PRs before a gated dispatch."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from api.database.execution import db_create_execution, db_link_execution_pull_requests
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.models.workspaces import Workspace
from api.plugins.sentry.fix_pr_reconcile import reconcile_open_fix_prs


def _setup_open_fix_pr(db: Session, provider: str = "github") -> tuple[Organization, PullRequest]:
    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    git_org = Organization(
        workspace_id=ws.id,
        name="git",
        external_org_id=f"g-{uuid.uuid4().hex[:6]}",
        provider=provider,
        installation_id="inst-1" if provider == "github" else None,
        auth_token_encrypted=None if provider == "github" else "enc-org",
        base_url=None,
    )
    db.add(git_org)
    db.commit()
    repo = Repository(
        org_id=git_org.id,
        name="acme/app",
        external_id="4242",
        provider=provider,
        web_url="https://example.test/acme/app",
        auth_token_encrypted=None,
    )
    db.add(repo)
    db.commit()
    issue = Issue(
        repository_id=repo.id,
        external_id="s-1",
        title="boom",
        level="error",
        triage_result=TriageResult.PENDING.value,
    )
    db.add(issue)
    db.commit()
    execution = db_create_execution(
        db,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=7,
        title="fix",
        author="jeanclode-bot",
        state="open",
        pr_url="https://example.test/acme/app/pull/7",
        head_branch="fix/s-1",
        base_branch="main",
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    db_link_execution_pull_requests(db, execution.id, [pr.id])
    return git_org, pr


def _db_plugin(db_session):
    plugin = MagicMock()
    plugin.run_in_session = AsyncMock(side_effect=lambda fn: fn(db_session))
    plugin.decrypt = MagicMock(return_value="tok")
    return plugin


@pytest.mark.asyncio
async def test_reconcile_flips_closed_pr_and_publishes(db_session):
    git_org, pr = _setup_open_fix_pr(db_session, provider="github")

    app = MagicMock()
    app.github.get_installation_access_token = AsyncMock(return_value="gh-tok")
    app.github.fetch_pull_request = AsyncMock(return_value={"state": "closed", "merged": False})

    with (
        patch("api.plugins.sentry.fix_pr_reconcile.get_current_app", return_value=app),
        patch(
            "api.plugins.sentry.fix_pr_reconcile.publish_pull_request_event",
            new=AsyncMock(),
        ) as mock_pub,
    ):
        await reconcile_open_fix_prs(_db_plugin(db_session), git_org.id)

    db_session.refresh(pr)
    assert pr.state == "closed"
    mock_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_keeps_db_state_on_api_failure(db_session):
    git_org, pr = _setup_open_fix_pr(db_session, provider="github")

    app = MagicMock()
    app.github.get_installation_access_token = AsyncMock(return_value="gh-tok")
    app.github.fetch_pull_request = AsyncMock(return_value=None)  # transient failure

    with (
        patch("api.plugins.sentry.fix_pr_reconcile.get_current_app", return_value=app),
        patch(
            "api.plugins.sentry.fix_pr_reconcile.publish_pull_request_event",
            new=AsyncMock(),
        ) as mock_pub,
    ):
        await reconcile_open_fix_prs(_db_plugin(db_session), git_org.id)

    db_session.refresh(pr)
    assert pr.state == "open"
    mock_pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_mints_github_token_once_for_many_prs(db_session):
    git_org, pr = _setup_open_fix_pr(db_session, provider="github")
    # A second open fix PR under the same git org.
    pr2 = PullRequest(
        repository_id=pr.repository_id,
        pr_number=8,
        title="fix 2",
        author="jeanclode-bot",
        state="open",
        pr_url="https://example.test/acme/app/pull/8",
        head_branch="fix/s-2",
        base_branch="main",
    )
    db_session.add(pr2)
    db_session.commit()
    from api.database.execution import db_create_execution, db_link_execution_pull_requests
    from api.models.executions import ExecutionWorkflow

    ex2 = db_create_execution(
        db_session,
        provider="sentry",
        issues=list(db_session.query(Issue).all()),
        workflow=ExecutionWorkflow.FIX.value,
    )
    db_link_execution_pull_requests(db_session, ex2.id, [pr2.id])

    app = MagicMock()
    app.github.get_installation_access_token = AsyncMock(return_value="gh-tok")
    app.github.fetch_pull_request = AsyncMock(return_value={"state": "open", "merged": False})

    with (
        patch("api.plugins.sentry.fix_pr_reconcile.get_current_app", return_value=app),
        patch(
            "api.plugins.sentry.fix_pr_reconcile.publish_pull_request_event",
            new=AsyncMock(),
        ),
    ):
        await reconcile_open_fix_prs(_db_plugin(db_session), git_org.id)

    app.github.get_installation_access_token.assert_awaited_once()
    assert app.github.fetch_pull_request.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_gitlab_merged(db_session):
    git_org, pr = _setup_open_fix_pr(db_session, provider="gitlab")

    app = MagicMock()
    app.gitlab.fetch_merge_request = AsyncMock(return_value={"state": "merged"})

    with (
        patch("api.plugins.sentry.fix_pr_reconcile.get_current_app", return_value=app),
        patch(
            "api.plugins.sentry.fix_pr_reconcile.publish_pull_request_event",
            new=AsyncMock(),
        ),
    ):
        await reconcile_open_fix_prs(_db_plugin(db_session), git_org.id)

    db_session.refresh(pr)
    assert pr.state == "merged"
