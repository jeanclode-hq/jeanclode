"""Part D — the CLI's fix PRs get persisted, linked, and light up issue status."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from api.database.dashboard import db_get_issue_detail, db_get_issues_paginated
from api.database.execution import db_create_execution
from api.database.repository import db_get_related_repos, db_update_repository_mapping
from api.models.execution_links import execution_pull_requests, issue_pull_requests
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import MappingMethod, Repository
from api.models.workspaces import Workspace
from api.plugins.sentry.consumer import _persist_pull_requests
from api.routers.issues.utils import issue_to_detail


def _setup(db: Session) -> tuple[Organization, Repository, Repository, Issue]:
    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    sentry_org = Organization(
        workspace_id=ws.id,
        name="sentry",
        external_org_id=f"s-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    git_org = Organization(
        workspace_id=ws.id,
        name="git",
        external_org_id=f"g-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add_all([sentry_org, git_org])
    db.commit()

    git_repo = Repository(
        org_id=git_org.id,
        name="acme/app",
        external_id=f"gr-{uuid.uuid4().hex[:6]}",
        provider="github",
        web_url="https://github.com/acme/app",
    )
    db.add(git_repo)
    db.commit()

    sentry_repo = Repository(
        org_id=sentry_org.id,
        name="app",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sentry_repo)
    db.commit()
    db_update_repository_mapping(db, sentry_repo.id, git_repo.id, MappingMethod.MANUAL.value)

    issue = Issue(
        repository_id=sentry_repo.id,
        external_id="sentry-42",
        title="boom",
        level="error",
        triage_result=TriageResult.PENDING.value,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return sentry_org, sentry_repo, git_repo, issue


def _result(issue_external_id: str, pr_url: str, pr_number: int) -> dict:
    return {
        "data": {
            "results": [
                {
                    "group": {"issue_ids": [issue_external_id], "root_cause": "x"},
                    "repos": {
                        "app": {
                            "pr_url": pr_url,
                            "pr_number": pr_number,
                            "provider": "github",
                            "head_branch": "fix/sentry-42",
                        }
                    },
                }
            ]
        }
    }


def test_persist_pull_requests_creates_and_links(db_session):
    _sentry_org, _sentry_repo, git_repo, issue = _setup(db_session)
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )

    _persist_pull_requests(
        db_session,
        execution.id,
        _result(issue.external_id, "https://github.com/acme/app/pull/7", 7),
    )

    pr = db_session.query(PullRequest).one()
    assert pr.repository_id == git_repo.id
    assert pr.pr_number == 7
    assert pr.state == "open"

    linked = db_session.execute(
        execution_pull_requests.select().where(
            execution_pull_requests.c.execution_id == execution.id
        )
    ).fetchall()
    assert [row.pull_request_id for row in linked] == [pr.id]


def test_persist_pull_requests_is_idempotent(db_session):
    _sentry_org, _sentry_repo, _git_repo, issue = _setup(db_session)
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    result = _result(issue.external_id, "https://github.com/acme/app/pull/7", 7)

    _persist_pull_requests(db_session, execution.id, result)
    _persist_pull_requests(db_session, execution.id, result)

    assert db_session.query(PullRequest).count() == 1
    linked = db_session.execute(
        execution_pull_requests.select().where(
            execution_pull_requests.c.execution_id == execution.id
        )
    ).fetchall()
    assert len(linked) == 1


def test_persist_pull_requests_matches_related_repo_by_web_url(db_session):
    """A multi-repo group's PR on a related (non-primary) repo links to that repo."""
    _sentry_org, _sentry_repo, git_repo, issue = _setup(db_session)

    # A second git repo, grouped with the primary via a repo-group mapping.
    lib = Repository(
        org_id=git_repo.org_id,
        name="acme/shared-lib",
        external_id=f"gr-{uuid.uuid4().hex[:6]}",
        provider="github",
        web_url="https://github.com/acme/shared-lib",
    )
    db_session.add(lib)
    db_session.commit()
    db_update_repository_mapping(db_session, git_repo.id, lib.id, MappingMethod.MANUAL.value)
    assert {r.id for r in db_get_related_repos(db_session, git_repo.id)} == {lib.id}

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    result = {
        "data": {
            "results": [
                {
                    "group": {"issue_ids": [issue.external_id]},
                    "repos": {
                        "app": {
                            "pr_url": "https://github.com/acme/app/pull/3",
                            "pr_number": 3,
                            "provider": "github",
                        },
                        "shared-lib": {
                            "pr_url": "https://github.com/acme/shared-lib/pull/9",
                            "pr_number": 9,
                            "provider": "github",
                        },
                    },
                }
            ]
        }
    }
    _persist_pull_requests(db_session, execution.id, result)

    by_repo = {pr.repository_id: pr.pr_number for pr in db_session.query(PullRequest).all()}
    assert by_repo == {git_repo.id: 3, lib.id: 9}
    assert len(execution.pull_requests) == 2


def test_persist_pull_requests_links_without_clobbering_webhook_row(db_session):
    """If the PR webhook already created the row, persist links it as-is."""
    _sentry_org, _sentry_repo, git_repo, issue = _setup(db_session)
    existing = PullRequest(
        repository_id=git_repo.id,
        pr_number=7,
        title="real title from webhook",
        author="dev",
        state="open",
        pr_url="https://github.com/acme/app/pull/7",
        head_branch="fix/sentry-42",
        base_branch="main",
    )
    db_session.add(existing)
    db_session.commit()

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    _persist_pull_requests(
        db_session,
        execution.id,
        _result(issue.external_id, "https://github.com/acme/app/pull/7", 7),
    )

    db_session.refresh(existing)
    assert existing.title == "real title from webhook"
    assert existing.base_branch == "main"
    assert db_session.query(PullRequest).count() == 1
    assert [pr.id for pr in execution.pull_requests] == [existing.id]


def test_persist_pull_requests_skips_entry_without_pr_number(db_session):
    _sentry_org, _sentry_repo, _git_repo, issue = _setup(db_session)
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    result = {
        "data": {
            "results": [
                {
                    "group": {"issue_ids": [issue.external_id]},
                    "repos": {"app": {"pr_url": "https://github.com/acme/app/pull/4"}},
                }
            ]
        }
    }
    _persist_pull_requests(db_session, execution.id, result)
    assert db_session.query(PullRequest).count() == 0


@pytest.mark.asyncio
async def test_handle_execution_status_wires_in_pr_persistence(db_session):
    """The terminal status consumer must actually call _persist_pull_requests."""
    from api.plugins.sentry import consumer as consumer_mod
    from api.plugins.sentry.consumer import ExecutionStatusMessage, _handle_execution_status

    fake_exec = MagicMock(status="completed", error_type=None, error_detail=None, issues=[])
    db_plugin = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    db_plugin.session.return_value = ctx
    app = MagicMock()
    app.database = db_plugin

    with (
        patch.object(consumer_mod, "get_current_app", return_value=app),
        patch.object(consumer_mod, "db_update_execution_status", return_value=fake_exec),
        patch.object(consumer_mod, "_persist_triage_results"),
        patch.object(consumer_mod, "_persist_pull_requests") as mock_persist,
        patch.object(consumer_mod, "_resolve_execution_targets", return_value=("", [])),
        patch.object(consumer_mod, "publish_execution_event", new=AsyncMock()),
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(
                execution_id=str(uuid.uuid4()),
                status="completed",
                result={"data": {"results": []}},
            )
        )
    mock_persist.assert_called_once()


def test_persist_pull_requests_scopes_each_pr_to_its_own_group_issues(db_session):
    """Reproduces the prod bug: a 3-issue batch split into 3 PRs (one per
    issue) must not cross-link — each issue should see only its own PR.
    """
    _sentry_org, sentry_repo, _git_repo, issue_a = _setup(db_session)
    issue_b = Issue(
        repository_id=sentry_repo.id,
        external_id="sentry-43",
        title="401",
        level="error",
        triage_result=TriageResult.PENDING.value,
    )
    issue_c = Issue(
        repository_id=sentry_repo.id,
        external_id="sentry-44",
        title="unknown",
        level="error",
        triage_result=TriageResult.PENDING.value,
    )
    db_session.add_all([issue_b, issue_c])
    db_session.commit()

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue_a, issue_b, issue_c],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    result = {
        "data": {
            "results": [
                {
                    "group": {"issue_ids": [issue_a.external_id]},
                    "repos": {
                        "app": {
                            "pr_url": "https://github.com/acme/app/pull/75",
                            "pr_number": 75,
                            "provider": "github",
                        }
                    },
                },
                {
                    "group": {"issue_ids": [issue_b.external_id]},
                    "repos": {
                        "app": {
                            "pr_url": "https://github.com/acme/app/pull/76",
                            "pr_number": 76,
                            "provider": "github",
                        }
                    },
                },
                {
                    "group": {"issue_ids": [issue_c.external_id]},
                    "repos": {
                        "app": {
                            "pr_url": "https://github.com/acme/app/pull/77",
                            "pr_number": 77,
                            "provider": "github",
                        }
                    },
                },
            ]
        }
    }

    _persist_pull_requests(db_session, execution.id, result)

    # Execution-level link is unchanged: all 3 PRs came out of this batch.
    assert len(execution.pull_requests) == 3

    # But issue_pull_requests scopes each PR to the one issue it addresses.
    links = db_session.execute(issue_pull_requests.select()).fetchall()
    prs_by_number = {pr.pr_number: pr for pr in db_session.query(PullRequest).all()}
    linked_pairs = {(row.issue_id, row.pull_request_id) for row in links}
    assert linked_pairs == {
        (issue_a.id, prs_by_number[75].id),
        (issue_b.id, prs_by_number[76].id),
        (issue_c.id, prs_by_number[77].id),
    }

    # And the API-facing read side reflects that: issue A's detail view
    # must show only PR !75, not the other two issues' PRs from the batch.
    detail_a = issue_to_detail(db_get_issue_detail(db_session, issue_a.id))
    fix_exec = next(e for e in detail_a.executions if e.workflow == "fix")
    assert [pr.pr_number for pr in fix_exec.pull_requests] == [75]


def test_linked_fix_pr_drives_issue_display_status(db_session):
    sentry_org, _sentry_repo, _git_repo, issue = _setup(db_session)
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.COMPLETED.value,
    )
    _persist_pull_requests(
        db_session,
        execution.id,
        _result(issue.external_id, "https://github.com/acme/app/pull/7", 7),
    )

    rows, _total = db_get_issues_paginated(db_session, sentry_org.workspace_id)
    statuses = {r[0].id: r[1] for r in rows}
    assert statuses[issue.id] == "pr_open"

    db_session.query(PullRequest).update({PullRequest.state: "merged"})
    db_session.commit()
    rows, _total = db_get_issues_paginated(db_session, sentry_org.workspace_id)
    assert {r[0].id: r[1] for r in rows}[issue.id] == "pr_merged"
