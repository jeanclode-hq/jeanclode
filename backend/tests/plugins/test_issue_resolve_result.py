"""issue_resolve results get persisted like Sentry fixes, without touching the Sentry merge gate."""

import uuid

from sqlalchemy.orm import Session

from api.database.execution import db_create_execution
from api.database.pull_request import db_git_org_has_open_fix_pr
from api.models.execution_links import execution_pull_requests, issue_pull_requests
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.models.workspaces import Workspace
from api.plugins.container.issue_resolve_result import persist_issue_resolve_result


def _setup(db: Session, provider: str = "github") -> tuple[Organization, Repository, Issue]:
    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    org = Organization(
        workspace_id=ws.id,
        name="acme",
        external_org_id=f"o-{uuid.uuid4().hex[:6]}",
        provider=provider,
    )
    db.add(org)
    db.commit()
    repo = Repository(
        org_id=org.id,
        name="acme/app",
        external_id=f"r-{uuid.uuid4().hex[:6]}",
        provider=provider,
        web_url="https://github.com/acme/app",
    )
    db.add(repo)
    db.commit()
    issue = Issue(
        repository_id=repo.id,
        external_id="12",
        title="Crash on save",
        level="info",
        triage_result=TriageResult.PENDING.value,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return org, repo, issue


def _execution(db: Session, issue: Issue, workflow: str = ExecutionWorkflow.ISSUE_RESOLVE.value):
    return db_create_execution(
        db,
        provider="github",
        issues=[issue],
        workflow=workflow,
        status=ExecutionStatus.COMPLETED.value,
    )


def _proceed(*pr_urls: str) -> dict:
    return {"data": {"kind": "proceed", "triage_result": "actionable", "pr_urls": list(pr_urls)}}


def test_links_pr_to_execution_and_issue(db_session):
    _org, repo, issue = _setup(db_session)
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session, execution.id, _proceed("https://github.com/acme/app/pull/7")
    )

    pr = db_session.query(PullRequest).one()
    assert (pr.repository_id, pr.pr_number, pr.state) == (repo.id, 7, "open")
    exec_links = db_session.execute(execution_pull_requests.select()).fetchall()
    assert [(r.execution_id, r.pull_request_id) for r in exec_links] == [(execution.id, pr.id)]
    issue_links = db_session.execute(issue_pull_requests.select()).fetchall()
    assert [(r.issue_id, r.pull_request_id) for r in issue_links] == [(issue.id, pr.id)]
    db_session.refresh(issue)
    assert issue.triage_result == TriageResult.ACTIONABLE.value


def test_reuses_pr_row_from_webhook_and_is_idempotent(db_session):
    _org, repo, issue = _setup(db_session)
    webhook_pr = PullRequest(
        repository_id=repo.id,
        pr_number=7,
        title="Real title",
        author="jeanclode-bot",
        pr_url="https://github.com/acme/app/pull/7",
        head_branch="resolve/12",
        base_branch="main",
    )
    db_session.add(webhook_pr)
    db_session.commit()
    execution = _execution(db_session, issue)
    result = _proceed("https://github.com/acme/app/pull/7")

    persist_issue_resolve_result(db_session, execution.id, result)
    persist_issue_resolve_result(db_session, execution.id, result)

    assert db_session.query(PullRequest).one().title == "Real title"
    assert len(db_session.execute(execution_pull_requests.select()).fetchall()) == 1
    assert len(db_session.execute(issue_pull_requests.select()).fetchall()) == 1


def test_gitlab_merge_request_url(db_session):
    _org, repo, issue = _setup(db_session, provider="gitlab")
    repo.web_url = "https://gitlab.example.com/acme/app"
    db_session.commit()
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session,
        execution.id,
        _proceed("https://gitlab.example.com/acme/app/-/merge_requests/3"),
    )

    assert db_session.query(PullRequest).one().pr_number == 3


def test_not_actionable_sets_triage_only(db_session):
    _org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session,
        execution.id,
        {"data": {"kind": "not_actionable", "triage_result": "not_actionable"}},
    )

    db_session.refresh(issue)
    assert issue.triage_result == TriageResult.NOT_ACTIONABLE.value
    assert db_session.query(PullRequest).count() == 0


def test_ignores_pr_outside_the_issue_repos(db_session):
    _org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session, execution.id, _proceed("https://github.com/someone-else/app/pull/7")
    )

    assert db_session.query(PullRequest).count() == 0


def test_other_workflows_are_left_alone(db_session):
    _org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue, workflow=ExecutionWorkflow.FIX.value)

    persist_issue_resolve_result(
        db_session, execution.id, _proceed("https://github.com/acme/app/pull/7")
    )

    assert db_session.query(PullRequest).count() == 0
    db_session.refresh(issue)
    assert issue.triage_result == TriageResult.PENDING.value


def test_sentry_issue_is_left_alone(db_session):
    _org, _repo, issue = _setup(db_session, provider="sentry")
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session, execution.id, _proceed("https://github.com/acme/app/pull/7")
    )

    assert db_session.query(PullRequest).count() == 0
    db_session.refresh(issue)
    assert issue.triage_result == TriageResult.PENDING.value


def test_open_issue_resolve_pr_does_not_hold_the_sentry_merge_gate(db_session):
    org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue)

    persist_issue_resolve_result(
        db_session, execution.id, _proceed("https://github.com/acme/app/pull/7")
    )

    assert db_session.query(PullRequest).one().state == "open"
    assert db_git_org_has_open_fix_pr(db_session, org.id) is False


def test_records_the_llm_the_fixer_ran_on(db_session):
    _org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue)
    result = _proceed()
    result["data"]["fixer_llm"] = {
        "credential": "self-hosted",
        "model": "qwen3-coder",
        "tier": "high",
        "reason": "The issue asks for the self-hosted model.",
    }

    persist_issue_resolve_result(db_session, execution.id, result)

    db_session.refresh(execution)
    assert (
        execution.fixer_llm_credential,
        execution.fixer_llm_model,
        execution.fixer_llm_reason,
    ) == ("self-hosted", "qwen3-coder", "The issue asks for the self-hosted model.")


def test_fixer_llm_is_ignored_on_other_workflows(db_session):
    _org, _repo, issue = _setup(db_session)
    execution = _execution(db_session, issue, workflow=ExecutionWorkflow.FIX.value)

    persist_issue_resolve_result(
        db_session, execution.id, {"data": {"fixer_llm": {"model": "claude-sonnet-5"}}}
    )

    db_session.refresh(execution)
    assert execution.fixer_llm_model is None
