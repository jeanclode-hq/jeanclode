"""Regression tests for the Execution ↔ Issue M2M dispatch behaviour."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from api.database.execution import (
    db_create_execution,
    db_get_dispatchable_issues,
    db_get_eligible_dispatch_targets,
    db_get_failed_execution_count,
    db_get_running_executions,
    db_has_active_execution,
    db_link_execution_pull_requests,
)
from api.database.organization import db_claim_dispatch_window
from api.database.repository import db_update_repository_mapping
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import MappingMethod, Repository
from api.models.workspaces import Workspace


def _make_org(db: Session) -> Organization:
    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    org = Organization(
        workspace_id=ws.id,
        name="sentry-org",
        external_org_id=f"sentry-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _make_repo(db: Session, org: Organization, *, mapped: bool = True) -> Repository:
    """Create a Sentry-side repo. If ``mapped`` is True, also create a git-side
    target repo and link the two via ``mapped_repo_id`` so the dispatcher
    treats the source as eligible.
    """
    git_repo_id = None
    if mapped:
        git_repo = Repository(
            org_id=org.id,
            name="git-proj",
            external_id=f"git-{uuid.uuid4().hex[:6]}",
            provider="github",
        )
        db.add(git_repo)
        db.commit()
        db.refresh(git_repo)
        git_repo_id = git_repo.id

    repo = Repository(
        org_id=org.id,
        name="proj",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)

    if git_repo_id:
        db_update_repository_mapping(db, repo.id, git_repo_id, MappingMethod.MANUAL.value)
        db.refresh(repo)

    return repo


def _make_issue(db: Session, repo: Repository, *, idx: int) -> Issue:
    issue = Issue(
        repository_id=repo.id,
        external_id=f"sentry-{idx}-{uuid.uuid4().hex[:6]}",
        title=f"Test issue {idx}",
        level="error",
        triage_result=TriageResult.PENDING.value,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def test_create_execution_links_n_issues(db_session):
    """One execution row + N rows in execution_issues for a batched fix."""
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    issues = [_make_issue(db_session, repo, idx=i) for i in range(3)]

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=issues,
        workflow=ExecutionWorkflow.FIX.value,
    )

    assert db_session.query(Execution).count() == 1
    assert {i.id for i in execution.issues} == {i.id for i in issues}
    for issue in issues:
        db_session.refresh(issue)
        assert len(issue.executions) == 1
        assert issue.executions[0].id == execution.id


def test_running_batch_blocks_all_linked_issues(db_session):
    """A running execution linking 2 issues blocks both from being dispatched."""
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    issues = [_make_issue(db_session, repo, idx=i) for i in range(2)]

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=issues,
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.RUNNING.value,
    )

    dispatchable = db_get_dispatchable_issues(db_session, org.id, limit=10, max_retries=3)
    assert dispatchable == []

    for issue in issues:
        assert db_has_active_execution(db_session, issue.id) is True

    # Flip to FAILED — both issues become eligible again (under retry cap)
    execution.status = ExecutionStatus.FAILED.value
    db_session.commit()

    dispatchable = db_get_dispatchable_issues(db_session, org.id, limit=10, max_retries=3)
    assert {i.id for i in dispatchable} == {i.id for i in issues}


def test_failed_count_isolated_per_issue(db_session):
    """A single failed execution linked to two issues counts once for each."""
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    issues = [_make_issue(db_session, repo, idx=i) for i in range(2)]

    db_create_execution(
        db_session,
        provider="sentry",
        issues=issues,
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.FAILED.value,
    )

    for issue in issues:
        assert db_get_failed_execution_count(db_session, issue.id) == 1


def test_unmapped_repo_skipped(db_session):
    """Issues on a repo with no ``mapped_repo_id`` are not dispatched."""
    org = _make_org(db_session)
    unmapped = _make_repo(db_session, org, mapped=False)
    issue = _make_issue(db_session, unmapped, idx=0)

    dispatchable = db_get_dispatchable_issues(db_session, org.id, limit=10, max_retries=3)
    assert dispatchable == []

    # Linking a target makes the same issue eligible.
    git_repo = Repository(
        org_id=org.id,
        name="g",
        external_id=f"g-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db_session.add(git_repo)
    db_session.commit()
    db_update_repository_mapping(db_session, unmapped.id, git_repo.id, MappingMethod.MANUAL.value)

    dispatchable = db_get_dispatchable_issues(db_session, org.id, limit=10, max_retries=3)
    assert [i.id for i in dispatchable] == [issue.id]


def test_retry_cap_excludes_at_max(db_session):
    """An issue with failed_count == max_retries is no longer dispatchable."""
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    issue = _make_issue(db_session, repo, idx=0)

    for _ in range(3):
        db_create_execution(
            db_session,
            provider="sentry",
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            status=ExecutionStatus.FAILED.value,
        )

    assert db_get_failed_execution_count(db_session, issue.id) == 3
    dispatchable = db_get_dispatchable_issues(db_session, org.id, limit=10, max_retries=3)
    assert dispatchable == []


def test_running_executions_scoped_by_provider(db_session):
    """A reconciler tick must only see its own plugin's executions —
    otherwise a github reconciler false-fails sentry-owned ones (#104)."""
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    sentry_issue = _make_issue(db_session, repo, idx=0)
    github_issue = _make_issue(db_session, repo, idx=1)

    sentry_exec = db_create_execution(
        db_session,
        provider="sentry",
        issues=[sentry_issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.RUNNING.value,
    )
    github_exec = db_create_execution(
        db_session,
        provider="github",
        issues=[github_issue],
        workflow=ExecutionWorkflow.RESPOND.value,
        status=ExecutionStatus.RUNNING.value,
    )

    sentry_running = db_get_running_executions(db_session, provider="sentry")
    github_running = db_get_running_executions(db_session, provider="github")

    assert [e.id for e in sentry_running] == [sentry_exec.id]
    assert [e.id for e in github_running] == [github_exec.id]


def _make_git_org(db: Session, sentry_org: Organization, *, name: str = "git-org") -> Organization:
    org = Organization(
        workspace_id=sentry_org.workspace_id,
        name=name,
        external_org_id=f"git-org-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _map_to_git_org(db: Session, sentry_repo: Repository, git_org: Organization) -> Repository:
    git_repo = Repository(
        org_id=git_org.id,
        name="acme/git-proj",
        external_id=f"git-{uuid.uuid4().hex[:6]}",
        provider="github",
        web_url="https://github.com/acme/git-proj",
    )
    db.add(git_repo)
    db.commit()
    db.refresh(git_repo)
    db_update_repository_mapping(db, sentry_repo.id, git_repo.id, MappingMethod.MANUAL.value)
    return git_repo


def _enable_automatic(db: Session, org: Organization) -> None:
    org.settings = {"triggers": {"triage": "automatic"}}
    db.commit()


def _make_git_subgroup(db: Session, root: Organization, *, name: str) -> Organization:
    """A subgroup org under ``root`` — mirrors ``_resolve_namespace_org``'s
    placeholder rows for a project living in a subgroup of a connected group.
    """
    org = Organization(
        workspace_id=root.workspace_id,
        name=name,
        external_org_id=f"git-subgroup-{uuid.uuid4().hex[:6]}",
        provider="github",
        parent_org_id=root.id,
        root_org_id=root.id,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def test_eligible_targets_fan_out_to_two_git_orgs(db_session):
    """One Sentry org whose projects map into two git orgs yields two pairs."""
    sentry_org = _make_org(db_session)
    _enable_automatic(db_session, sentry_org)

    git_org_a = _make_git_org(db_session, sentry_org, name="org-a")
    git_org_b = _make_git_org(db_session, sentry_org, name="org-b")

    repo_a = _make_repo(db_session, sentry_org, mapped=False)
    repo_b = _make_repo(db_session, sentry_org, mapped=False)
    _map_to_git_org(db_session, repo_a, git_org_a)
    _map_to_git_org(db_session, repo_b, git_org_b)
    _make_issue(db_session, repo_a, idx=0)
    _make_issue(db_session, repo_b, idx=1)

    targets = db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3)
    assert set(targets) == {(sentry_org.id, git_org_a.id), (sentry_org.id, git_org_b.id)}

    # The batch selection is scoped to a single pair.
    batch_a = db_get_dispatchable_issues(db_session, sentry_org.id, 10, 3, git_org_id=git_org_a.id)
    assert {i.repository_id for i in batch_a} == {repo_a.id}


def test_eligible_targets_skips_manual_triage(db_session):
    sentry_org = _make_org(db_session)  # settings default → manual
    git_org = _make_git_org(db_session, sentry_org)
    repo = _make_repo(db_session, sentry_org, mapped=False)
    _map_to_git_org(db_session, repo, git_org)
    _make_issue(db_session, repo, idx=0)

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == []


def test_gate_blocks_pair_while_a_fix_pr_is_open(db_session):
    """gate_on_open_fix_prs: an open FIX-linked PR under the git org drops the pair."""
    sentry_org = _make_org(db_session)
    sentry_org.settings = {
        "triggers": {"triage": "automatic"},
        "gate_on_open_fix_prs": True,
    }
    db_session.commit()

    git_org = _make_git_org(db_session, sentry_org)
    sentry_repo = _make_repo(db_session, sentry_org, mapped=False)
    git_repo = _map_to_git_org(db_session, sentry_repo, git_org)
    issue = _make_issue(db_session, sentry_repo, idx=0)

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == [
        (sentry_org.id, git_org.id)
    ]

    # A FIX execution with an OPEN linked PR under this git org closes the gate.
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.FAILED.value,
    )
    pr = PullRequest(
        repository_id=git_repo.id,
        pr_number=1,
        title="fix",
        author="jeanclode-bot",
        state=PRState.OPEN.value,
        pr_url="https://github.com/acme/git-proj/pull/1",
        head_branch="fix/1",
        base_branch="main",
    )
    db_session.add(pr)
    db_session.commit()
    db_link_execution_pull_requests(db_session, execution.id, [pr.id])

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == []

    # Closing the PR re-opens the gate.
    pr.state = PRState.CLOSED.value
    db_session.commit()
    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == [
        (sentry_org.id, git_org.id)
    ]


def test_running_fix_batch_excludes_its_git_org_but_not_others(db_session):
    """The git org is the perimeter: a RUNNING fix execution drops that git
    org's pair from eligibility; a different git org is untouched."""
    sentry_org = _make_org(db_session)
    _enable_automatic(db_session, sentry_org)

    git_org_a = _make_git_org(db_session, sentry_org, name="a")
    git_org_b = _make_git_org(db_session, sentry_org, name="b")
    # git_org_a has two Sentry projects; one issue goes RUNNING, the other
    # stays fresh — so git_org_a still has a dispatchable issue.
    repo_a1 = _make_repo(db_session, sentry_org, mapped=False)
    repo_a2 = _make_repo(db_session, sentry_org, mapped=False)
    repo_b = _make_repo(db_session, sentry_org, mapped=False)
    _map_to_git_org(db_session, repo_a1, git_org_a)
    _map_to_git_org(db_session, repo_a2, git_org_a)
    _map_to_git_org(db_session, repo_b, git_org_b)
    issue_a1 = _make_issue(db_session, repo_a1, idx=0)
    _make_issue(db_session, repo_a2, idx=1)
    _make_issue(db_session, repo_b, idx=2)

    assert set(db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3)) == {
        (sentry_org.id, git_org_a.id),
        (sentry_org.id, git_org_b.id),
    }

    db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue_a1],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.RUNNING.value,
    )

    # git_org_a is held by the running batch even though repo_a2's issue is
    # still dispatchable; git_org_b is untouched.
    targets = db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3)
    assert targets == [(sentry_org.id, git_org_b.id)]


def test_gate_off_ignores_open_fix_prs(db_session):
    sentry_org = _make_org(db_session)
    _enable_automatic(db_session, sentry_org)  # gate_on_open_fix_prs absent → off

    git_org = _make_git_org(db_session, sentry_org)
    sentry_repo = _make_repo(db_session, sentry_org, mapped=False)
    git_repo = _map_to_git_org(db_session, sentry_repo, git_org)
    issue = _make_issue(db_session, sentry_repo, idx=0)

    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.FAILED.value,
    )
    pr = PullRequest(
        repository_id=git_repo.id,
        pr_number=2,
        title="fix",
        author="jeanclode-bot",
        state=PRState.OPEN.value,
        pr_url="https://github.com/acme/git-proj/pull/2",
        head_branch="fix/2",
        base_branch="main",
    )
    db_session.add(pr)
    db_session.commit()
    db_link_execution_pull_requests(db_session, execution.id, [pr.id])

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == [
        (sentry_org.id, git_org.id)
    ]


def test_claim_dispatch_window_atomic_with_int_minutes(db_session):
    """The window claim now takes a Python int for minutes (resolved from the
    Sentry org's batch_window) rather than a SQL CASE — exercise it for real
    so ``func.make_interval`` with a bound int is proven against Postgres."""
    git_org = _make_git_org(db_session, _make_org(db_session))

    assert db_claim_dispatch_window(db_session, git_org.id, 60) is True
    # Still inside the 60-minute window → second claim fails.
    assert db_claim_dispatch_window(db_session, git_org.id, 60) is False

    # Push last_dispatched_at back past the window → claim succeeds again.
    git_org.last_dispatched_at = datetime.now(UTC) - timedelta(minutes=61)
    db_session.commit()
    assert db_claim_dispatch_window(db_session, git_org.id, 60) is True

    # A longer window (1 week) still blocks a fresh claim.
    assert db_claim_dispatch_window(db_session, git_org.id, 10080) is False


def test_gate_blocks_sibling_subgroup_sharing_root(db_session):
    """An open FIX PR under one subgroup must hold the gate closed for a
    sibling subgroup connected under the same root — they share one
    git-platform token, so the merge gate has to span the whole root, not
    stop at each subgroup's own org row (see ``org_effective_root_id``)."""
    sentry_org = _make_org(db_session)
    sentry_org.settings = {
        "triggers": {"triage": "automatic"},
        "gate_on_open_fix_prs": True,
    }
    db_session.commit()

    root = _make_git_org(db_session, sentry_org, name="root")
    subgroup_a = _make_git_subgroup(db_session, root, name="subgroup-a")
    subgroup_b = _make_git_subgroup(db_session, root, name="subgroup-b")

    repo_a = _make_repo(db_session, sentry_org, mapped=False)
    repo_b = _make_repo(db_session, sentry_org, mapped=False)
    git_repo_a = _map_to_git_org(db_session, repo_a, subgroup_a)
    _map_to_git_org(db_session, repo_b, subgroup_b)
    issue_a = _make_issue(db_session, repo_a, idx=0)
    _make_issue(db_session, repo_b, idx=1)

    # Both subgroups fold into a single (sentry_org, root) pair.
    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == [
        (sentry_org.id, root.id)
    ]

    # A PR opened under subgroup_a (via a FIX execution on issue_a) must hold
    # the gate closed for subgroup_b too — same root, same token.
    execution = db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue_a],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.FAILED.value,
    )
    pr = PullRequest(
        repository_id=git_repo_a.id,
        pr_number=1,
        title="fix",
        author="jeanclode-bot",
        state=PRState.OPEN.value,
        pr_url="https://github.com/acme/git-proj/pull/1",
        head_branch="fix/1",
        base_branch="main",
    )
    db_session.add(pr)
    db_session.commit()
    db_link_execution_pull_requests(db_session, execution.id, [pr.id])

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == []


def test_running_fix_batch_blocks_sibling_subgroup_sharing_root(db_session):
    """The concurrency perimeter is the connected root, not each subgroup:
    a RUNNING fix execution under one subgroup must drop the whole root's
    pair, holding back a sibling subgroup sharing the same token."""
    sentry_org = _make_org(db_session)
    _enable_automatic(db_session, sentry_org)

    root = _make_git_org(db_session, sentry_org, name="root")
    subgroup_a = _make_git_subgroup(db_session, root, name="subgroup-a")
    subgroup_b = _make_git_subgroup(db_session, root, name="subgroup-b")

    repo_a = _make_repo(db_session, sentry_org, mapped=False)
    repo_b = _make_repo(db_session, sentry_org, mapped=False)
    _map_to_git_org(db_session, repo_a, subgroup_a)
    _map_to_git_org(db_session, repo_b, subgroup_b)
    issue_a = _make_issue(db_session, repo_a, idx=0)
    _make_issue(db_session, repo_b, idx=1)

    db_create_execution(
        db_session,
        provider="sentry",
        issues=[issue_a],
        workflow=ExecutionWorkflow.FIX.value,
        status=ExecutionStatus.RUNNING.value,
    )

    assert db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3) == []


def test_disabled_git_target_blocks_sentry_dispatch(db_session):
    """Disabling the mapped git repo stops Sentry auto-fixes landing in it.

    The toggle lives on the git repo, so a Sentry issue whose target is off
    must drop out of the dispatch batch — otherwise "disabled" would only
    cover the git triggers and Sentry would keep opening PRs there.
    """
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    issue = _make_issue(db_session, repo, idx=0)

    assert [i.id for i in db_get_dispatchable_issues(db_session, org.id, 10, 3)] == [issue.id]

    git_repo = db_session.query(Repository).filter(Repository.provider == "github").one()
    git_repo.settings = {"enabled": False}
    db_session.commit()

    assert db_get_dispatchable_issues(db_session, org.id, 10, 3) == []

    git_repo.settings = {"enabled": True}
    db_session.commit()

    assert [i.id for i in db_get_dispatchable_issues(db_session, org.id, 10, 3)] == [issue.id]
