"""Tests for workspaces dashboard endpoints."""

import uuid

from api.database import db_create_workspace, db_create_workspace_membership
from api.database.repository import db_update_repository_mapping
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization, OrgMembership
from api.models.pull_requests import PullRequest
from api.models.repositories import MappingMethod, Repository


def _setup_workspace_with_stats(db, mock_auth):
    """Create workspace with issues and executions for stats testing."""
    ws = db_create_workspace(db=db, name="stats-ws", slug=f"stats-ws-{uuid.uuid4().hex[:6]}")
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

    git_org = Organization(
        workspace_id=ws.id,
        name="test-org",
        external_org_id=f"ext-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add(git_org)
    db.flush()

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
        name="stats-org",
        external_org_id=f"stats-org-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sentry_org)
    db.flush()

    sp = Repository(
        org_id=sentry_org.id,
        name="stats-project",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(sp)
    db.flush()
    db_update_repository_mapping(db, sp.id, repo.id, MappingMethod.MANUAL.value)

    def _make_issue(idx, triage_result=TriageResult.ACTIONABLE.value):
        issue = Issue(
            repository_id=sp.id,
            external_id=f"SENTRY-STAT-{idx}-{uuid.uuid4().hex[:4]}",
            title=f"Stats issue #{idx}",
            level="error",
            triage_result=triage_result,
        )
        db.add(issue)
        db.flush()
        return issue

    # 2 pending (actionable, no executions)
    _make_issue(0)
    _make_issue(1)

    def _add_exec(*, issues, pull_requests=None, status):
        ex = Execution(provider="sentry", workflow="fix", status=status)
        ex.issues = list(issues)
        if pull_requests:
            ex.pull_requests = list(pull_requests)
        db.add(ex)
        return ex

    # 1 running
    i2 = _make_issue(2)
    _add_exec(issues=[i2], status=ExecutionStatus.RUNNING.value)

    # 1 pr_open (completed execution + open PR)
    i3 = _make_issue(3)
    pr_open = PullRequest(
        repository_id=repo.id,
        pr_number=100,
        title="Fix #3",
        author="bot",
        state="open",
        pr_url="https://github.com/org/repo/pull/100",
        head_branch="fix/3",
        base_branch="main",
    )
    db.add(pr_open)
    db.flush()
    _add_exec(issues=[i3], pull_requests=[pr_open], status=ExecutionStatus.COMPLETED.value)

    # 2 pr_merged
    for idx in [4, 5]:
        i = _make_issue(idx)
        pr = PullRequest(
            repository_id=repo.id,
            pr_number=100 + idx,
            title=f"Fix #{idx}",
            author="bot",
            state="merged",
            pr_url=f"https://github.com/org/repo/pull/{100 + idx}",
            head_branch=f"fix/{idx}",
            base_branch="main",
        )
        db.add(pr)
        db.flush()
        _add_exec(issues=[i], pull_requests=[pr], status=ExecutionStatus.COMPLETED.value)

    # 1 not_actionable
    _make_issue(6, triage_result=TriageResult.NOT_ACTIONABLE.value)

    # 1 rejected (completed execution + closed PR)
    i7 = _make_issue(7)
    pr_closed = PullRequest(
        repository_id=repo.id,
        pr_number=107,
        title="Fix #7",
        author="bot",
        state="closed",
        pr_url="https://github.com/org/repo/pull/107",
        head_branch="fix/7",
        base_branch="main",
    )
    db.add(pr_closed)
    db.flush()
    _add_exec(issues=[i7], pull_requests=[pr_closed], status=ExecutionStatus.COMPLETED.value)

    # 1 failed
    i8 = _make_issue(8)
    _add_exec(issues=[i8], status=ExecutionStatus.FAILED.value)

    db.commit()
    db.refresh(ws)
    return ws


def test_workspace_stats(auth_client, app, mock_auth):
    """GET /workspaces/{id}/stats returns correct counts."""
    with app.database.session() as db:
        ws = _setup_workspace_with_stats(db, mock_auth)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/stats")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_issues"] == 9
    assert data["pending"] == 2
    assert data["running"] == 1
    assert data["pr_open"] == 1
    assert data["pr_merged"] == 2
    assert data["not_actionable"] == 1
    assert data["rejected"] == 1
    assert data["failed"] == 1
    # pr_success_rate = 2 / (2 + 1 + 1) = 0.5
    assert data["pr_success_rate"] == 0.5


def test_workspace_stats_empty(auth_client, app, mock_auth):
    """GET /workspaces/{id}/stats returns zeros for workspace with no issues."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="empty-ws", slug=f"empty-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_issues"] == 0
    assert data["pr_success_rate"] == 0.0


def test_workspace_stats_forbidden(auth_client, app, mock_auth):
    """GET /workspaces/{id}/stats returns 403 for non-member."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other-ws", slug=f"other-ws-{uuid.uuid4().hex[:6]}")
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/stats")
    assert resp.status_code == 403


def test_workspace_executions(auth_client, app, mock_auth):
    """GET /workspaces/{id}/executions serializes real rows (regression: ex.current_step doesn't exist on Execution)."""
    with app.database.session() as db:
        ws = _setup_workspace_with_stats(db, mock_auth)
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/executions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 6
    assert all(item["current_step"] is None for item in data["items"])


def test_workspace_sources_includes_org_with_token(auth_client, app, mock_auth):
    """GET /workspaces/{id}/sources includes orgs with their own token."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="src-ws", slug=f"src-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        org = Organization(
            workspace_id=ws.id,
            name="sentry-org",
            external_org_id=f"ext-{uuid.uuid4().hex[:6]}",
            provider="sentry",
            auth_token_encrypted="encrypted-token",
        )
        db.add(org)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["sources"]]
    assert names == ["sentry-org"]


def test_workspace_sources_includes_tokenless_org_with_repo(auth_client, app, mock_auth):
    """GET /workspaces/{id}/sources includes an org with no org-level token as
    long as it has at least one repository — e.g. a GitLab namespace connected
    via a project access token, whose token lives on the Repository row.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="proj-token-ws", slug=f"pt-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        namespace_org = Organization(
            workspace_id=ws.id,
            name="examplecorp",
            external_org_id=f"ns-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted=None,
        )
        db.add(namespace_org)
        db.flush()

        repo = Repository(
            org_id=namespace_org.id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="examplecorp/project",
            provider="gitlab",
            auth_token_encrypted="repo-level-token",
        )
        db.add(repo)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["sources"]]
    assert names == ["examplecorp"]


def test_workspace_sources_excludes_placeholder_ancestor_org(auth_client, app, mock_auth):
    """GET /workspaces/{id}/sources excludes hierarchy-only orgs — no token
    and no repositories of their own.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="placeholder-ws", slug=f"ph-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        placeholder = Organization(
            workspace_id=ws.id,
            name="root-group",
            external_org_id=f"root-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted=None,
        )
        db.add(placeholder)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


def test_workspace_sources_excludes_subgroup_under_tokened_group(auth_client, app, mock_auth):
    """GET /workspaces/{id}/sources lists the connected GitLab group only, not
    the subgroups its token already covers.

    A group token is added once, for one group; the repo sync then creates an
    org per subgroup namespace to hang that subgroup's projects off, copying
    the group token onto each project row. Those subgroups were never
    connected separately, so offering them as sources would invent
    connections the tenant never made.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="subgroup-ws", slug=f"sg-ws-{uuid.uuid4().hex[:6]}")
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

        subgroup = Organization(
            workspace_id=ws.id,
            name="accessly",
            external_org_id=f"sub-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted=None,
            parent_org_id=group.id,
            root_org_id=group.id,
        )
        db.add(subgroup)
        db.flush()

        nested = Organization(
            workspace_id=ws.id,
            name="accessly-nested",
            external_org_id=f"nest-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted=None,
            parent_org_id=subgroup.id,
            root_org_id=group.id,
        )
        db.add(nested)
        db.flush()

        for org in (subgroup, nested):
            db.add(
                Repository(
                    org_id=org.id,
                    external_id=f"repo-{uuid.uuid4().hex[:6]}",
                    name=f"{org.name}/project",
                    provider="gitlab",
                    # The group sync copies the group token onto every project
                    # row, subgroups included — so a repo token proves nothing
                    # about whether this org is its own connection.
                    auth_token_encrypted="group-token",
                )
            )
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["sources"]]
    assert names == ["team-crm-automation"]


def test_workspace_sources_includes_subgroup_when_only_it_has_a_token(auth_client, app, mock_auth):
    """GET /workspaces/{id}/sources lists a subgroup that holds the token, with
    its token-less parent left out as the placeholder it is.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="sub-only-ws", slug=f"so-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        parent = Organization(
            workspace_id=ws.id,
            name="team-crm-automation",
            external_org_id=f"grp-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted=None,
        )
        db.add(parent)
        db.flush()

        subgroup = Organization(
            workspace_id=ws.id,
            name="accessly",
            external_org_id=f"sub-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted="subgroup-token",
            parent_org_id=parent.id,
            root_org_id=parent.id,
        )
        db.add(subgroup)
        db.commit()
        ws_id = str(ws.id)

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["sources"]]
    assert names == ["accessly"]


def test_deleting_a_group_takes_its_subgroups_with_it(auth_client, app, mock_auth):
    """Deleting a connected GitLab group deletes the subgroup orgs beneath it.

    Regression test: the hierarchy FKs used to be ON DELETE SET NULL, so
    removing the group left every subgroup behind as a root of its own —
    each one then read as a separate connection, turning one deleted group
    into dozens of phantom ones.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="cascade-ws", slug=f"cd-ws-{uuid.uuid4().hex[:6]}")
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

        nested = Organization(
            workspace_id=ws.id,
            name="accessly-inner",
            external_org_id=f"nest-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            parent_org_id=subgroup.id,
            root_org_id=group.id,
        )
        db.add(nested)
        db.flush()

        db.add(
            Repository(
                org_id=nested.id,
                external_id=f"repo-{uuid.uuid4().hex[:6]}",
                name="accessly/inner/api",
                provider="gitlab",
                auth_token_encrypted="group-token",
            )
        )
        db.commit()
        ws_id, group_id = str(ws.id), str(group.id)
        subtree_ids = [group.id, subgroup.id, nested.id]

    resp = auth_client.delete(f"/organizations/{group_id}")
    assert resp.status_code == 200

    with app.database.session() as db:
        remaining = db.query(Organization).filter(Organization.id.in_(subtree_ids)).all()
        assert remaining == []

    resp = auth_client.get(f"/workspaces/{ws_id}/sources")
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


def test_deleting_a_subgroup_prunes_its_placeholder_ancestors(auth_client, app, mock_auth):
    """Deleting the only connected org under a placeholder removes the placeholder.

    Placeholder ancestors are created purely to carry the hierarchy; once
    nothing hangs off them they are invisible rows that nothing can reach.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="prune-ws", slug=f"pr-ws-{uuid.uuid4().hex[:6]}")
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=mock_auth.id)

        placeholder = Organization(
            workspace_id=ws.id,
            name="examplecorp",
            external_org_id=f"root-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
        )
        db.add(placeholder)
        db.flush()

        subgroup = Organization(
            workspace_id=ws.id,
            name="accessly",
            external_org_id=f"sub-{uuid.uuid4().hex[:6]}",
            provider="gitlab",
            auth_token_encrypted="subgroup-token",
            parent_org_id=placeholder.id,
            root_org_id=placeholder.id,
        )
        db.add(subgroup)
        db.flush()
        db.add(
            OrgMembership(
                org_id=subgroup.id, provider_identity_id=mock_auth.identity_id, role="owner"
            )
        )
        db.commit()
        subgroup_id, placeholder_id = str(subgroup.id), placeholder.id

    resp = auth_client.delete(f"/organizations/{subgroup_id}")
    assert resp.status_code == 200

    with app.database.session() as db:
        assert db.query(Organization).filter(Organization.id == placeholder_id).first() is None


def test_deleting_a_git_org_unmaps_sentry_projects_without_deleting_them(
    auth_client, app, mock_auth
):
    """Deleting a GitLab group must not take the Sentry side with it.

    The group owns its subgroups and their repositories, and those go. A
    Sentry project that merely *pointed* at one of them is not part of the
    connection being removed — it survives, unmapped, ready to be pointed
    somewhere else.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="unmap-ws", slug=f"um-ws-{uuid.uuid4().hex[:6]}")
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

        git_repo = Repository(
            org_id=subgroup.id,
            external_id=f"repo-{uuid.uuid4().hex[:6]}",
            name="accessly/api",
            provider="gitlab",
        )
        db.add(git_repo)
        db.flush()

        sentry_org = Organization(
            workspace_id=ws.id,
            name="sentry-org",
            external_org_id=f"sen-{uuid.uuid4().hex[:6]}",
            provider="sentry",
            auth_token_encrypted="sentry-token",
        )
        db.add(sentry_org)
        db.flush()

        sentry_project = Repository(
            org_id=sentry_org.id,
            external_id=f"proj-{uuid.uuid4().hex[:6]}",
            name="accessly-api",
            provider="sentry",
        )
        db.add(sentry_project)
        db.flush()

        db_update_repository_mapping(db, sentry_project.id, git_repo.id, MappingMethod.FUZZY.value)
        db.commit()
        group_id = str(group.id)
        sentry_org_id, sentry_project_id = sentry_org.id, sentry_project.id
        git_repo_id, subgroup_id = git_repo.id, subgroup.id

    resp = auth_client.delete(f"/organizations/{group_id}")
    assert resp.status_code == 200

    with app.database.session() as db:
        # The group's side is gone, subgroup and its repository included.
        assert db.query(Organization).filter(Organization.id == subgroup_id).first() is None
        assert db.query(Repository).filter(Repository.id == git_repo_id).first() is None

        # The Sentry side survives, pointing at nothing.
        assert db.query(Organization).filter(Organization.id == sentry_org_id).first() is not None
        project = db.query(Repository).filter(Repository.id == sentry_project_id).first()
        assert project is not None
        assert project.mapped_repo_id is None
