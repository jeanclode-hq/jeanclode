"""Tests for issues dashboard endpoints."""

import uuid
from datetime import UTC, datetime, timedelta

from api.database import (
    db_create_workspace,
    db_create_workspace_membership,
)
from api.database.execution import db_create_execution
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization, OrgMembership
from api.models.repositories import Repository


def _setup_workspace_with_issues(db, mock_auth, *, issue_count=3, triage_result="actionable"):
    """Helper to create workspace -> organization -> source_project -> issues chain."""
    ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

    sentry_org = Organization(
        workspace_id=ws.id,
        name="test-sentry-org",
        external_org_id=f"test-org-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sentry_org)
    db.flush()

    db.add(
        OrgMembership(
            org_id=sentry_org.id, provider_identity_id=mock_auth.identity_id, role="owner"
        )
    )

    sp = Repository(
        org_id=sentry_org.id,
        name="my-project",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sp)
    db.flush()

    issues = []
    for i in range(issue_count):
        issue = Issue(
            repository_id=sp.id,
            external_id=f"SENTRY-{uuid.uuid4().hex[:6]}",
            title=f"Error #{i}",
            level="error",
            triage_result=triage_result,
            event_count=i + 1,
        )
        db.add(issue)
        issues.append(issue)

    db.commit()
    for obj in [ws, sentry_org, sp, *issues]:
        db.refresh(obj)

    return ws, sentry_org, sp, issues


# =============================================================================
# GET /issues — list
# =============================================================================


def test_list_issues(auth_client, app, mock_auth):
    """GET /issues returns paginated issues for a workspace."""
    with app.database.session() as db:
        ws, _, _, _issues = _setup_workspace_with_issues(db, mock_auth, issue_count=3)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 3
    assert data["pagination"]["total"] == 3
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["limit"] == 25


def test_list_issues_pagination(auth_client, app, mock_auth):
    """GET /issues respects page and limit params."""
    with app.database.session() as db:
        ws, _, _, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=10)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&page=2&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 5
    assert data["pagination"]["total"] == 10
    assert data["pagination"]["page"] == 2


def test_list_issues_status_filter(auth_client, app, mock_auth):
    """GET /issues filters by computed status."""
    with app.database.session() as db:
        ws, _, sp, _ = _setup_workspace_with_issues(
            db, mock_auth, issue_count=2, triage_result="actionable"
        )
        # Add an issue with a running execution
        running_issue = Issue(
            repository_id=sp.id,
            external_id="SENTRY-RUNNING",
            title="Running error",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
        )
        db.add(running_issue)
        db.flush()
        execution = Execution(
            provider="sentry",
            workflow="fix",
            status=ExecutionStatus.RUNNING.value,
        )
        execution.issues = [running_issue]
        db.add(execution)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 3  # 2 pending + 1 running, all "open"
    for item in data["objects"]:
        assert item["status"] == "unresolved"


def test_list_issues_period_filter_uses_last_seen(auth_client, app, mock_auth):
    """GET /issues?period=7days must match on recurrence (last_seen), not
    first_seen — a long-lived issue that recurred yesterday belongs in the
    last-7-days view even though it was first created months ago."""
    with app.database.session() as db:
        ws, _, sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=0)
        now = datetime.now(UTC)
        recurring = Issue(
            repository_id=sp.id,
            external_id="SENTRY-RECURRING",
            title="Recurring error",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
            first_seen=now - timedelta(days=90),
            last_seen=now - timedelta(hours=11),
        )
        stale = Issue(
            repository_id=sp.id,
            external_id="SENTRY-STALE",
            title="Stale error",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
            first_seen=now - timedelta(days=90),
            last_seen=now - timedelta(days=90),
        )
        db.add_all([recurring, stale])
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&period=7days")
    assert resp.status_code == 200
    titles = {item["title"] for item in resp.json()["objects"]}
    assert titles == {"Recurring error"}


def test_list_issues_sorted_by_first_seen(auth_client, app, mock_auth):
    """Rows follow the provider's creation date, not our insertion time: backfills
    store old issues late, and a recent recurrence must not bump an old issue."""
    with app.database.session() as db:
        ws, _, sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=0)
        now = datetime.now(UTC)
        for title, first_seen, last_seen in [
            ("Fresh", now - timedelta(hours=2), now - timedelta(hours=2)),
            ("Old but recurring", now - timedelta(days=30), now - timedelta(minutes=5)),
            ("Backfilled", now - timedelta(days=7), now - timedelta(days=7)),
        ]:
            db.add(
                Issue(
                    repository_id=sp.id,
                    external_id=f"SENTRY-{title}",
                    title=title,
                    level="error",
                    triage_result=TriageResult.ACTIONABLE.value,
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
            )
            db.commit()
        db.add(
            Issue(
                repository_id=sp.id,
                external_id="SENTRY-NO-FIRST-SEEN",
                title="No first_seen",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    assert [item["title"] for item in resp.json()["objects"]] == [
        "No first_seen",
        "Fresh",
        "Backfilled",
        "Old but recurring",
    ]


def test_list_issues_search(auth_client, app, mock_auth):
    """GET /issues filters by title search."""
    with app.database.session() as db:
        ws, _, sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=0)
        db.add(
            Issue(
                repository_id=sp.id,
                external_id="SENTRY-A",
                title="TypeError: cannot read property",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.add(
            Issue(
                repository_id=sp.id,
                external_id="SENTRY-B",
                title="ReferenceError: x is not defined",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&search=TypeError")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 1
    assert "TypeError" in data["objects"][0]["title"]


def test_list_issues_forbidden(auth_client, app, mock_auth):
    """GET /issues returns 403 for workspace user is not a member of."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other-ws", slug="other-ws")
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 403


def test_list_issues_response_fields(auth_client, app, mock_auth):
    """GET /issues returns expected fields in each issue."""
    with app.database.session() as db:
        ws, _, _, _issues = _setup_workspace_with_issues(db, mock_auth, issue_count=1)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    item = resp.json()["objects"][0]
    assert "id" in item
    assert "external_id" in item
    assert "title" in item
    assert "status" in item
    assert "project" in item
    assert "created_at" in item
    assert item["project"] == "my-project"


def test_list_issues_response_includes_execution_id(auth_client, app, mock_auth):
    """The list item's execution_id is the issue's latest execution — needed
    by the frontend to cancel a running one without a separate detail fetch."""
    with app.database.session() as db:
        _ws, _, _, issues = _setup_workspace_with_issues(db, mock_auth, issue_count=1)
        ws_id = str(_ws.id)
        execution = db_create_execution(
            db,
            provider="sentry",
            issues=[issues[0]],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.RUNNING.value,
        )
        execution_id = str(execution.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    item = resp.json()["objects"][0]
    assert item["execution_id"] == execution_id
    assert item["execution_status"] == "running"


def test_issue_status_ignores_respond_runs(auth_client, app, mock_auth):
    """A respond run on the issue must not surface as its status, list or detail."""
    with app.database.session() as db:
        _ws, _, _, issues = _setup_workspace_with_issues(db, mock_auth, issue_count=1)
        ws_id, issue_id = str(_ws.id), str(issues[0].id)
        fix = db_create_execution(
            db,
            provider="sentry",
            issues=[issues[0]],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.COMPLETED.value,
        )
        respond = db_create_execution(
            db,
            provider="sentry",
            issues=[issues[0]],
            workflow=ExecutionWorkflow.RESPOND.value,
            status=ExecutionStatus.RUNNING.value,
        )
        respond.created_at = fix.created_at + timedelta(minutes=1)
        db.commit()
        fix_id = str(fix.id)

    item = auth_client.get(f"/issues?workspace_id={ws_id}").json()["objects"][0]
    assert item["execution_status"] == "completed"
    assert item["execution_id"] == fix_id
    assert item["workflow"] == ExecutionWorkflow.FIX.value

    detail = auth_client.get(f"/issues/{issue_id}").json()
    assert detail["execution_status"] == "completed"
    assert detail["workflow"] == ExecutionWorkflow.FIX.value
    assert [e["id"] for e in detail["executions"]] == [fix_id]


# =============================================================================
# GET /issues/{issue_id} — detail
# =============================================================================


def test_get_issue_detail(auth_client, app, mock_auth):
    """GET /issues/{id} returns issue detail with triage_metadata."""
    with app.database.session() as db:
        _ws, _, _, issues = _setup_workspace_with_issues(db, mock_auth, issue_count=1)
        issue = issues[0]
        issue.triage_metadata = {"actionable": True, "reason": "test"}
        db.commit()
        db.refresh(issue)
        issue_id = str(issue.id)

    resp = auth_client.get(f"/issues/{issue_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["triage_metadata"] == {"actionable": True, "reason": "test"}


def test_get_issue_detail_not_found(auth_client, app, mock_auth):
    """GET /issues/{id} returns 404 for non-existent issue."""
    resp = auth_client.get(f"/issues/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_issue_detail_forbidden(auth_client, app, mock_auth):
    """GET /issues/{id} returns 403 when user lacks workspace access."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other-ws", slug=f"other-{uuid.uuid4().hex[:6]}")
        sentry_org = Organization(
            workspace_id=ws.id,
            name="other-org",
            external_org_id=f"other-org-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(sentry_org)
        db.flush()
        sp = Repository(
            org_id=sentry_org.id,
            name="other-proj",
            external_id="sp-other",
            provider="sentry",
        )
        db.add(sp)
        db.flush()
        issue = Issue(
            repository_id=sp.id,
            external_id="SENTRY-NOPE",
            title="Forbidden error",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)
        issue_id = str(issue.id)

    resp = auth_client.get(f"/issues/{issue_id}")
    assert resp.status_code == 403


# =============================================================================
# POST /issues/{issue_id}/retry
# =============================================================================


def test_retry_issue_with_failed_execution(auth_client, app, mock_auth):
    """POST /issues/{id}/retry sets triage_result back to actionable."""
    with app.database.session() as db:
        _ws, _, _sp, issues = _setup_workspace_with_issues(
            db, mock_auth, issue_count=1, triage_result="actionable"
        )
        issue = issues[0]
        # Create a failed execution
        failed_exec = Execution(
            provider="sentry",
            workflow="fix",
            status=ExecutionStatus.FAILED.value,
            error_type="test_failure",
        )
        failed_exec.issues = [issue]
        db.add(failed_exec)
        db.commit()
        issue_id = str(issue.id)

    resp = auth_client.post(f"/issues/{issue_id}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unresolved"


def test_retry_dismissed_issue(auth_client, app, mock_auth):
    """POST /issues/{id}/retry can re-activate a dismissed issue."""
    with app.database.session() as db:
        _ws, _, _, issues = _setup_workspace_with_issues(
            db, mock_auth, issue_count=1, triage_result="not_actionable"
        )
        issue_id = str(issues[0].id)

    resp = auth_client.post(f"/issues/{issue_id}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["triage_result"] == "actionable"


def test_retry_issue_not_found(auth_client, app, mock_auth):
    """POST /issues/{id}/retry returns 404 for non-existent issue."""
    resp = auth_client.post(f"/issues/{uuid.uuid4()}/retry")
    assert resp.status_code == 404


# =============================================================================
# POST /issues/{issue_id}/dismiss
# =============================================================================


def test_dismiss_issue(auth_client, app, mock_auth):
    """POST /issues/{id}/dismiss sets triage_result to not_actionable."""
    with app.database.session() as db:
        _ws, _, _, issues = _setup_workspace_with_issues(
            db, mock_auth, issue_count=1, triage_result="actionable"
        )
        issue_id = str(issues[0].id)

    resp = auth_client.post(f"/issues/{issue_id}/dismiss")
    assert resp.status_code == 200
    data = resp.json()
    assert data["triage_result"] == "not_actionable"


def test_dismiss_issue_not_found(auth_client, app, mock_auth):
    """POST /issues/{id}/dismiss returns 404 for non-existent issue."""
    resp = auth_client.post(f"/issues/{uuid.uuid4()}/dismiss")
    assert resp.status_code == 404


def test_list_issues_has_source_field(auth_client, app, mock_auth):
    """GET /issues returns source field in each issue."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)
        sentry_org = Organization(
            workspace_id=ws.id,
            name="test-org",
            external_org_id=f"test-org-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(sentry_org)
        db.flush()
        db.add(
            OrgMembership(
                org_id=sentry_org.id, provider_identity_id=mock_auth.identity_id, role="owner"
            )
        )
        sp = Repository(
            org_id=sentry_org.id,
            name="proj",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(sp)
        db.flush()
        db.add(
            Issue(
                repository_id=sp.id,
                external_id="SENTRY-SRC",
                title="Error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    item = resp.json()["objects"][0]
    assert item["source"] == "sentry"


# =============================================================================
# Org-membership scoping
# =============================================================================


def test_list_issues_repository_filter(auth_client, app, mock_auth):
    """GET /issues filters by repository_id."""
    with app.database.session() as db:
        ws, sentry_org, sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=2)

        other_sp = Repository(
            org_id=sentry_org.id,
            name="other-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_sp)
        db.flush()
        db.add(
            Issue(
                repository_id=other_sp.id,
                external_id="SENTRY-OTHER-PROJECT",
                title="Other project error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)
        sp_id = str(sp.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&repository_id={sp_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 2
    for item in data["objects"]:
        assert item["project"] == "my-project"


# =============================================================================
# GET /issues/repositories — filter options
# =============================================================================


def test_list_issue_repositories(auth_client, app, mock_auth):
    """GET /issues/repositories returns distinct repos that have issues."""
    with app.database.session() as db:
        ws, sentry_org, sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=2)

        # A repo with no issues should not show up
        empty_repo = Repository(
            org_id=sentry_org.id,
            name="empty-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(empty_repo)
        db.commit()
        ws_id = str(ws.id)
        sp_id = str(sp.id)

    resp = auth_client.get(f"/issues/repositories?workspace_id={ws_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == [{"id": sp_id, "name": "my-project", "org_name": "test-sentry-org"}]
    assert data["total"] == 1
    assert data["has_more"] is False


def test_list_issue_repositories_span_every_org_in_the_workspace(auth_client, app, mock_auth):
    """Workspace membership is the whole check — provider memberships don't narrow it."""
    with app.database.session() as db:
        ws, _, _sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=1)

        other_org = Organization(
            workspace_id=ws.id,
            name="other-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_org)
        db.flush()
        other_sp = Repository(
            org_id=other_org.id,
            name="other-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_sp)
        db.flush()
        db.add(
            Issue(
                repository_id=other_sp.id,
                external_id="SENTRY-OTHER",
                title="Invisible error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues/repositories?workspace_id={ws_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert sorted(repo["name"] for repo in data["items"]) == ["my-project", "other-project"]


def test_list_issue_repositories_scoped_by_source_org_id(auth_client, app, mock_auth):
    """GET /issues/repositories?source_org_id=... only returns repos from that org.

    Regression test: the repo filter dropdown used to ignore the selected
    org tab entirely and always return repos across every org the user can
    access, so switching org tabs never changed the dropdown's contents.
    """
    with app.database.session() as db:
        ws, sentry_org, _, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=1)

        other_org = Organization(
            workspace_id=ws.id,
            name="other-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_org)
        db.flush()
        db.add(
            OrgMembership(
                org_id=other_org.id, provider_identity_id=mock_auth.identity_id, role="owner"
            )
        )
        other_sp = Repository(
            org_id=other_org.id,
            name="other-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_sp)
        db.flush()
        db.add(
            Issue(
                repository_id=other_sp.id,
                external_id="SENTRY-OTHER",
                title="Other org error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)
        sentry_org_id = str(sentry_org.id)
        other_org_id = str(other_org.id)

    # User is a member of both orgs, so the unscoped list includes both repos.
    resp = auth_client.get(f"/issues/repositories?workspace_id={ws_id}")
    assert {repo["name"] for repo in resp.json()["items"]} == {"my-project", "other-project"}

    resp = auth_client.get(
        f"/issues/repositories?workspace_id={ws_id}&source_org_id={sentry_org_id}"
    )
    assert resp.status_code == 200
    assert [repo["name"] for repo in resp.json()["items"]] == ["my-project"]

    resp = auth_client.get(
        f"/issues/repositories?workspace_id={ws_id}&source_org_id={other_org_id}"
    )
    assert resp.status_code == 200
    assert [repo["name"] for repo in resp.json()["items"]] == ["other-project"]


def test_list_issue_repositories_source_org_id_needs_no_org_membership(auth_client, app, mock_auth):
    """An org the user holds no provider membership on still answers on its own tab."""
    with app.database.session() as db:
        ws, _, _sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=1)

        other_org = Organization(
            workspace_id=ws.id,
            name="other-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_org)
        db.flush()
        other_sp = Repository(
            org_id=other_org.id,
            name="other-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_sp)
        db.flush()
        db.add(
            Issue(
                repository_id=other_sp.id,
                external_id="SENTRY-OTHER",
                title="Invisible error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)
        other_org_id = str(other_org.id)

    resp = auth_client.get(
        f"/issues/repositories?workspace_id={ws_id}&source_org_id={other_org_id}"
    )
    assert resp.status_code == 200
    assert [repo["name"] for repo in resp.json()["items"]] == ["other-project"]
    assert resp.json()["total"] == 1


def test_list_issue_repositories_pagination_and_search(auth_client, app, mock_auth):
    """GET /issues/repositories pages by ``page``/``limit`` and filters by ``search``.

    Server-side pagination and search keep the repo filter dropdown
    responsive for workspaces with many repositories.
    """
    with app.database.session() as db:
        ws = db_create_workspace(
            db=db, name="pg-issues-ws", slug=f"pg-issues-{uuid.uuid4().hex[:6]}"
        )
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        sentry_org = Organization(
            workspace_id=ws.id,
            name="pg-sentry-org",
            external_org_id=f"pg-org-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(sentry_org)
        db.flush()
        db.add(
            OrgMembership(
                org_id=sentry_org.id, provider_identity_id=mock_auth.identity_id, role="owner"
            )
        )

        for i in range(30):
            repo = Repository(
                org_id=sentry_org.id,
                name=f"repo-{i:02d}",
                external_id=f"pg-sp-{uuid.uuid4().hex[:6]}",
                provider="sentry",
            )
            db.add(repo)
            db.flush()
            db.add(
                Issue(
                    repository_id=repo.id,
                    external_id=f"SENTRY-{uuid.uuid4().hex[:6]}",
                    title=f"Error in repo-{i:02d}",
                    level="error",
                    triage_result=TriageResult.ACTIONABLE.value,
                )
            )
        db.commit()
        ws_id = str(ws.id)

    page1 = auth_client.get(f"/issues/repositories?workspace_id={ws_id}&page=1&limit=25").json()
    assert page1["total"] == 30
    assert page1["has_more"] is True
    assert [r["name"] for r in page1["items"]][:2] == ["repo-00", "repo-01"]
    assert len(page1["items"]) == 25

    page2 = auth_client.get(f"/issues/repositories?workspace_id={ws_id}&page=2&limit=25").json()
    assert page2["has_more"] is False
    assert len(page2["items"]) == 5

    filtered = auth_client.get(f"/issues/repositories?workspace_id={ws_id}&search=repo-1").json()
    assert filtered["total"] == 10
    assert all("repo-1" in r["name"] for r in filtered["items"])


def test_list_issues_span_every_org_in_the_workspace(auth_client, app, mock_auth):
    """Every org in the workspace shows up, membership on the provider or not."""
    with app.database.session() as db:
        ws, _, _sp, _ = _setup_workspace_with_issues(db, mock_auth, issue_count=2)

        # Second org in the same workspace — current user has no OrgMembership
        other_org = Organization(
            workspace_id=ws.id,
            name="other-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_org)
        db.flush()
        other_sp = Repository(
            org_id=other_org.id,
            name="other-project",
            external_id=f"sp-{uuid.uuid4().hex[:6]}",
            provider="sentry",
        )
        db.add(other_sp)
        db.flush()
        db.add(
            Issue(
                repository_id=other_sp.id,
                external_id="SENTRY-OTHER",
                title="Invisible error",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 3
    assert {item["project"] for item in data["objects"]} == {"my-project", "other-project"}


def test_list_issues_rolls_up_gitlab_subgroups(auth_client, app, mock_auth):
    """Issues under a GitLab subgroup belong to the connected group.

    The subgroup org exists only because the repo sync needed somewhere to
    hang the subgroup's projects; it carries neither a membership nor a
    source tab of its own. Its issues must still be reachable — unfiltered,
    and when filtering by the group the user actually connected.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="rollup-ws", slug=f"ru-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        group = Organization(
            workspace_id=ws.id,
            name="team-crm-automation",
            external_org_id=f"grp-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted="group-token",
        )
        db.add(group)
        db.flush()
        db.add(
            OrgMembership(org_id=group.id, provider_identity_id=mock_auth.identity_id, role="owner")
        )

        subgroup = Organization(
            workspace_id=ws.id,
            name="accessly",
            external_org_id=f"sub-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            parent_org_id=group.id,
            root_org_id=group.id,
        )
        db.add(subgroup)
        db.flush()

        sub_repo = Repository(
            org_id=subgroup.id,
            name="accessly/api",
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
        )
        db.add(sub_repo)
        db.flush()

        db.add(
            Issue(
                repository_id=sub_repo.id,
                external_id="GL-1",
                title="Subgroup issue",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)
        group_id = str(group.id)

    resp = auth_client.get(f"/issues?workspace_id={ws_id}")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["objects"]] == ["Subgroup issue"]

    resp = auth_client.get(f"/issues?workspace_id={ws_id}&source_org_id={group_id}")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["objects"]] == ["Subgroup issue"]


def test_list_issue_repositories_rolls_up_gitlab_subgroups(auth_client, app, mock_auth):
    """The repo filter dropdown for a connected group includes subgroup projects."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="rollup-repo-ws", slug=f"rr-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        group = Organization(
            workspace_id=ws.id,
            name="team-crm-automation",
            external_org_id=f"grp-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted="group-token",
        )
        db.add(group)
        db.flush()
        db.add(
            OrgMembership(org_id=group.id, provider_identity_id=mock_auth.identity_id, role="owner")
        )

        subgroup = Organization(
            workspace_id=ws.id,
            name="accessly",
            external_org_id=f"sub-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            parent_org_id=group.id,
            root_org_id=group.id,
        )
        db.add(subgroup)
        db.flush()

        sub_repo = Repository(
            org_id=subgroup.id,
            name="accessly/api",
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
        )
        db.add(sub_repo)
        db.flush()

        db.add(
            Issue(
                repository_id=sub_repo.id,
                external_id="GL-2",
                title="Subgroup issue",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)
        group_id = str(group.id)

    resp = auth_client.get(f"/issues/repositories?workspace_id={ws_id}&source_org_id={group_id}")
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()["items"]] == ["accessly/api"]
