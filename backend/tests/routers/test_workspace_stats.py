"""Stat card definitions: dashboard window, top pingers, and the PR page."""

import uuid
from datetime import UTC, datetime, timedelta

from api.database import db_create_workspace, db_create_workspace_membership
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.identities import ProviderIdentity
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository

RUNNING = ExecutionStatus.RUNNING.value
QUEUED = ExecutionStatus.QUEUED.value
COMPLETED = ExecutionStatus.COMPLETED.value
FAILED = ExecutionStatus.FAILED.value
REVIEW = ExecutionWorkflow.REVIEW.value
SUMMARY = ExecutionWorkflow.SUMMARY.value
RESPOND = ExecutionWorkflow.RESPOND.value


def _workspace(db, user_id=None):
    name = f"ws-{uuid.uuid4().hex[:6]}"
    ws = db_create_workspace(db=db, name=name, slug=name)
    if user_id:
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=user_id)
    org = Organization(workspace_id=ws.id, name=name, external_org_id=name, provider="github")
    db.add(org)
    db.flush()
    repo = Repository(org_id=org.id, name=f"{name}/app", external_id=name, provider="github")
    db.add(repo)
    db.flush()
    return ws, repo


def _pr(db, repo, number, *, state="open"):
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=number,
        title=f"PR {number}",
        author="alice",
        state=state,
        pr_url=f"https://github.com/{repo.name}/pull/{number}",
        head_branch=f"feat/{number}",
        base_branch="main",
    )
    db.add(pr)
    db.flush()
    return pr


def _run(db, pr, workflow, status, *, days_ago=0, identity=None):
    ex = Execution(
        provider="github",
        workflow=workflow,
        status=status,
        triggered_by_identity_id=identity.id if identity else None,
    )
    ex.pull_requests = [pr]
    ex.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    db.add(ex)
    db.flush()
    return ex


def _identity(db, username):
    identity = ProviderIdentity(
        provider="github",
        external_id=f"gh-{uuid.uuid4().hex[:8]}",
        username=username,
        avatar_url=f"https://avatars.example/{username}",
    )
    db.add(identity)
    db.flush()
    return identity


def test_dashboard_and_pull_request_cards(auth_client, app, mock_auth):
    with app.database.session() as db:
        ws, repo = _workspace(db, mock_auth.id)
        reviewed_twice = _pr(db, repo, 1)
        _run(db, reviewed_twice, REVIEW, COMPLETED)
        _run(db, reviewed_twice, REVIEW, COMPLETED)
        _run(db, reviewed_twice, SUMMARY, COMPLETED)
        reviewed_long_ago = _pr(db, repo, 2, state="merged")
        _run(db, reviewed_long_ago, REVIEW, COMPLETED, days_ago=45)
        review_failed = _pr(db, repo, 3)
        _run(db, review_failed, REVIEW, FAILED)
        never_reviewed = _pr(db, repo, 4)
        _run(db, never_reviewed, REVIEW, RUNNING)
        _run(db, never_reviewed, SUMMARY, QUEUED)
        _pr(db, repo, 5, state="closed")

        disabled_repo = Repository(
            org_id=repo.org_id,
            name="off",
            external_id=f"off-{uuid.uuid4().hex[:6]}",
            provider="github",
            settings={"enabled": False},
        )
        db.add(disabled_repo)
        db.flush()
        _pr(db, disabled_repo, 6)

        alice, bob, carol = (_identity(db, n) for n in ("alice", "bob", "carol"))
        for _ in range(3):
            _run(db, review_failed, RESPOND, COMPLETED, identity=alice)
        _run(db, review_failed, RESPOND, FAILED, identity=carol)
        _run(db, review_failed, RESPOND, COMPLETED, identity=carol)
        _run(db, review_failed, RESPOND, COMPLETED, identity=bob)
        _run(db, review_failed, RESPOND, COMPLETED, identity=bob, days_ago=45)
        _run(db, review_failed, RESPOND, COMPLETED, identity=bob, days_ago=45)
        _run(db, review_failed, RESPOND, COMPLETED)
        for bot in ("group_40_bot_6f11260438", "project_7_bot_ab12"):
            for _ in range(5):
                _run(db, review_failed, RESPOND, COMPLETED, identity=_identity(db, bot))

        _other_ws, other_repo = _workspace(db)
        other_pr = _pr(db, other_repo, 1)
        for _ in range(5):
            _run(db, other_pr, RESPOND, COMPLETED, identity=bob)
        _run(db, other_pr, REVIEW, RUNNING)
        db.commit()
        ws_id = str(ws.id)

    data = auth_client.get(f"/workspaces/{ws_id}/stats").json()

    dashboard = data["dashboard"]
    assert (dashboard["running"], dashboard["queued"]) == (1, 1)
    # 2 reviews + 1 summary + 3 alice + 1 carol + 1 bob + 1 anonymous ping + 10 bot pings; old and failed runs aren't.
    assert dashboard["successful_runs"] == 19
    assert dashboard["reviewed_prs"] == 1
    assert [(u["username"], u["pings"]) for u in dashboard["top_users"]] == [
        ("alice", 3),
        ("carol", 2),
    ]
    assert dashboard["top_users"][0]["avatar_url"] == "https://avatars.example/alice"

    assert data["pull_requests"] == {
        "reviewed": 2,
        # A failed review isn't a review; the closed PR and the disabled repo's PR don't wait on one.
        "pending_review": 2,
        "summarized": 1,
    }
