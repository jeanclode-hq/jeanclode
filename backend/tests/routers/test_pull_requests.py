"""Tests for pull requests list endpoint."""

import uuid

from api.database import db_create_workspace, db_create_workspace_membership
from api.database.repository import db_update_repository_mapping
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization, OrgMembership
from api.models.pull_requests import PullRequest
from api.models.repositories import MappingMethod, Repository


def _setup_workspace_with_prs(db, mock_auth, *, count=2):
    """Create workspace chain with issues, executions, and pull requests."""
    ws = db_create_workspace(db=db, name="test-ws", slug=f"test-ws-{uuid.uuid4().hex[:6]}")
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

    git_org = Organization(
        workspace_id=ws.id,
        name="test-org",
        external_org_id=f"ext-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add(git_org)
    db.flush()
    db.add(
        OrgMembership(org_id=git_org.id, provider_identity_id=mock_auth.identity_id, role="owner")
    )

    repo = Repository(
        org_id=git_org.id,
        external_id=f"repo-{uuid.uuid4().hex[:6]}",
        name="my-repo",
        provider="github",
    )
    db.add(repo)
    db.flush()

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
    db_update_repository_mapping(db, sp.id, repo.id, MappingMethod.MANUAL.value)

    executions = []
    for i in range(count):
        issue = Issue(
            repository_id=sp.id,
            external_id=f"SENTRY-PR-{uuid.uuid4().hex[:6]}",
            title=f"Bug #{i}",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
        )
        db.add(issue)
        db.flush()

        pr = PullRequest(
            repository_id=repo.id,
            pr_number=i + 1,
            title=f"Fix bug #{i}",
            author="jeanclode-bot",
            state="open",
            pr_url=f"https://github.com/org/my-repo/pull/{i + 1}",
            head_branch=f"fix/sentry-{i + 1}",
            base_branch="main",
        )
        db.add(pr)
        db.flush()

        # PR row badge tracks REVIEW executions (the PR's own workflow);
        # the upstream FIX execution that created the PR is reflected on
        # the issue, not the PR.
        execution = Execution(
            provider="github",
            workflow="review",
            status=ExecutionStatus.COMPLETED.value,
        )
        execution.pull_requests = [pr]
        db.add(execution)
        executions.append(execution)

    db.commit()
    for obj in [ws, git_org, repo, sentry_org, sp, *executions]:
        db.refresh(obj)

    return ws, repo, sp, executions


# =============================================================================
# GET /workspaces/{workspace_id}/pull-requests — list
# =============================================================================


def test_list_pull_requests(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests returns paginated PRs."""
    with app.database.session() as db:
        ws, _, _, _ = _setup_workspace_with_prs(db, mock_auth, count=3)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 3
    assert data["pagination"]["total"] == 3
    assert data["pagination"]["page"] == 1


def test_list_pull_requests_response_fields(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests returns expected fields."""
    with app.database.session() as db:
        ws, _, _, executions = _setup_workspace_with_prs(db, mock_auth, count=1)
        ws_id = str(ws.id)
        execution_id = str(executions[0].id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests")
    assert resp.status_code == 200
    item = resp.json()["objects"][0]
    assert "id" in item
    assert "title" in item
    assert "author" in item
    assert "pr_url" in item
    assert "pr_number" in item
    assert "branch_name" in item
    assert "repo_name" in item
    assert item["repo_name"] == "my-repo"
    assert "status" in item
    assert item["status"] == "open"
    assert "execution_status" in item
    assert item["execution_status"] == "completed"
    assert item["execution_id"] == execution_id
    assert "created_at" in item


def test_list_pull_requests_status_filter(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests filters by PR state."""
    with app.database.session() as db:
        ws, repo, sp, _ = _setup_workspace_with_prs(db, mock_auth, count=2)
        # Add a merged PR
        issue = Issue(
            repository_id=sp.id,
            external_id="SENTRY-MERGED",
            title="Merged bug",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
        )
        db.add(issue)
        db.flush()
        merged_pr = PullRequest(
            repository_id=repo.id,
            pr_number=99,
            title="Fix merged bug",
            author="jeanclode-bot",
            state="merged",
            pr_url="https://github.com/org/my-repo/pull/99",
            head_branch="fix/sentry-99",
            base_branch="main",
        )
        db.add(merged_pr)
        db.flush()
        merged_exec = Execution(
            provider="sentry",
            workflow="fix",
            status=ExecutionStatus.COMPLETED.value,
        )
        merged_exec.issues = [issue]
        merged_exec.pull_requests = [merged_pr]
        db.add(merged_exec)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests?status=merged")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 1
    assert data["objects"][0]["status"] == "merged"


def test_list_pull_requests_excludes_no_pr(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests excludes issues without PR/execution."""
    with app.database.session() as db:
        ws, _, sp, _ = _setup_workspace_with_prs(db, mock_auth, count=1)
        # Add issue without PR
        db.add(
            Issue(
                repository_id=sp.id,
                external_id="SENTRY-NOPR",
                title="No PR issue",
                level="error",
                triage_result=TriageResult.ACTIONABLE.value,
            )
        )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["objects"]) == 1  # Only the one with PR


def test_list_pull_requests_forbidden(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests returns 403 for non-member."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other-ws", slug=f"other-{uuid.uuid4().hex[:6]}")
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests")
    assert resp.status_code == 403


# =============================================================================
# Org-membership scoping
# =============================================================================


def test_list_pull_requests_span_every_org_in_the_workspace(auth_client, app, mock_auth):
    """Workspace membership is the whole check — provider memberships don't narrow it."""
    with app.database.session() as db:
        ws, _, _, _ = _setup_workspace_with_prs(db, mock_auth, count=1)

        # Second git org in the same workspace — current user has no OrgMembership
        other_org = Organization(
            workspace_id=ws.id,
            name="other-git-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(other_org)
        db.flush()
        other_repo = Repository(
            org_id=other_org.id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="other-repo",
            provider="github",
        )
        db.add(other_repo)
        db.flush()
        other_pr = PullRequest(
            repository_id=other_repo.id,
            pr_number=999,
            title="Invisible PR",
            author="other-bot",
            state="open",
            pr_url="https://github.com/other/repo/pull/999",
            head_branch="fix/999",
            base_branch="main",
        )
        db.add(other_pr)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 2
    assert {pr["repo_name"] for pr in data["objects"]} == {"my-repo", "other-repo"}


# =============================================================================
# repository_id filter
# =============================================================================


def test_list_pull_requests_repository_id_filter(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests?repository_id=... scopes to one repo."""
    with app.database.session() as db:
        ws, repo, _, _ = _setup_workspace_with_prs(db, mock_auth, count=1)

        git_org = db.query(Organization).filter(Organization.id == repo.org_id).first()
        other_repo = Repository(
            org_id=git_org.id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="other-repo",
            provider="github",
        )
        db.add(other_repo)
        db.flush()
        db.add(
            PullRequest(
                repository_id=other_repo.id,
                pr_number=42,
                title="Fix in other repo",
                author="jeanclode-bot",
                state="open",
                pr_url="https://github.com/org/other-repo/pull/42",
                head_branch="fix/42",
                base_branch="main",
            )
        )
        db.commit()
        ws_id = str(ws.id)
        repo_id = str(repo.id)
        other_repo_id = str(other_repo.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests?repository_id={repo_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 1
    assert data["objects"][0]["repo_name"] == "my-repo"

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests?repository_id={other_repo_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 1
    assert data["objects"][0]["repo_name"] == "other-repo"


# =============================================================================
# GET /workspaces/{workspace_id}/pull-requests/repositories — filter options
# =============================================================================


def test_list_pull_request_repositories(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests/repositories returns distinct repos that have PRs."""
    with app.database.session() as db:
        ws, repo, _, _ = _setup_workspace_with_prs(db, mock_auth, count=2)

        # A repo with no PRs should not show up
        empty_repo = Repository(
            org_id=repo.org_id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="empty-repo",
            provider="github",
        )
        db.add(empty_repo)
        db.commit()
        ws_id = str(ws.id)
        repo_id = str(repo.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests/repositories")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == [{"id": repo_id, "name": "my-repo", "org_name": "test-org"}]
    assert data["total"] == 1
    assert data["has_more"] is False


def test_list_pull_request_repositories_scoped_by_org_id(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests/repositories?org_id=... only returns repos from that org."""
    with app.database.session() as db:
        ws, repo, _, _ = _setup_workspace_with_prs(db, mock_auth, count=1)
        git_org_id = repo.org_id

        other_org = Organization(
            workspace_id=ws.id,
            name="other-git-org",
            external_org_id=f"other-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(other_org)
        db.flush()
        db.add(
            OrgMembership(
                org_id=other_org.id, provider_identity_id=mock_auth.identity_id, role="owner"
            )
        )
        other_repo = Repository(
            org_id=other_org.id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="other-repo",
            provider="github",
        )
        db.add(other_repo)
        db.flush()
        db.add(
            PullRequest(
                repository_id=other_repo.id,
                pr_number=1,
                title="Fix in other org",
                author="jeanclode-bot",
                state="open",
                pr_url="https://github.com/other/other-repo/pull/1",
                head_branch="fix/1",
                base_branch="main",
            )
        )
        db.commit()
        ws_id = str(ws.id)
        other_org_id = str(other_org.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests/repositories")
    assert {repo["name"] for repo in resp.json()["items"]} == {"my-repo", "other-repo"}

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests/repositories?org_id={git_org_id}")
    assert [repo["name"] for repo in resp.json()["items"]] == ["my-repo"]

    resp = auth_client.get(f"/workspaces/{ws_id}/pull-requests/repositories?org_id={other_org_id}")
    assert [repo["name"] for repo in resp.json()["items"]] == ["other-repo"]


def test_list_pull_request_repositories_pagination_and_search(auth_client, app, mock_auth):
    """GET /workspaces/{ws}/pull-requests/repositories pages by ``page``/``limit`` and ``search``."""
    with app.database.session() as db:
        ws, repo, _, _ = _setup_workspace_with_prs(db, mock_auth, count=0)
        git_org_id = repo.org_id

        for i in range(30):
            pg_repo = Repository(
                org_id=git_org_id,
                external_id=f"pg-repo-{uuid.uuid4().hex[:6]}",
                name=f"repo-{i:02d}",
                provider="github",
            )
            db.add(pg_repo)
            db.flush()
            db.add(
                PullRequest(
                    repository_id=pg_repo.id,
                    pr_number=i + 1,
                    title=f"Fix #{i}",
                    author="jeanclode-bot",
                    state="open",
                    pr_url=f"https://github.com/org/repo-{i:02d}/pull/1",
                    head_branch=f"fix/{i}",
                    base_branch="main",
                )
            )
        db.commit()
        ws_id = str(ws.id)

    page1 = auth_client.get(
        f"/workspaces/{ws_id}/pull-requests/repositories?page=1&limit=25"
    ).json()
    assert page1["total"] == 30
    assert page1["has_more"] is True
    assert [r["name"] for r in page1["items"]][:2] == ["repo-00", "repo-01"]
    assert len(page1["items"]) == 25

    page2 = auth_client.get(
        f"/workspaces/{ws_id}/pull-requests/repositories?page=2&limit=25"
    ).json()
    assert page2["has_more"] is False
    assert len(page2["items"]) == 5

    filtered = auth_client.get(
        f"/workspaces/{ws_id}/pull-requests/repositories?search=repo-1"
    ).json()
    assert filtered["total"] == 10
    assert all("repo-1" in r["name"] for r in filtered["items"])
