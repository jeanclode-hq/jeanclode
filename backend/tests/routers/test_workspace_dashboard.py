"""Dashboard read path: cached stats and the active-executions endpoint."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.database import db_create_workspace, db_create_workspace_membership
from api.database.dashboard import ACTIVE_EXECUTION_LIMITS
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.sse.publishers import publish_execution_event
from api.sse.stats_cache import get_cached_stats, invalidate_stats, store_stats
from tests.routers.test_pool_usage import _peak_checked_out
from tests.routers.test_workspaces import _setup_workspace_with_stats


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def mget(self, *keys: str) -> list[bytes | None]:
        return [self.data.get(key) for key in keys]

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value.encode()

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, b"0")) + 1
        self.data[key] = str(value).encode()
        return value

    async def expire(self, key: str, seconds: int) -> None:
        pass


@pytest.fixture
def fake_redis():
    fake = FakeRedis()
    with patch("api.sse.stats_cache._redis", return_value=fake):
        yield fake


def _workspace(db, user_id, *, member: bool = True):
    name = f"dash-{uuid.uuid4().hex[:6]}"
    ws = db_create_workspace(db=db, name=name, slug=name)
    if member:
        db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=user_id)
    org = Organization(workspace_id=ws.id, name=name, external_org_id=name, provider="github")
    db.add(org)
    db.flush()
    repo = Repository(org_id=org.id, name=f"{name}/app", external_id=name, provider="github")
    db.add(repo)
    db.flush()
    return ws, repo


def _issue(db, repo):
    issue = Issue(
        repository_id=repo.id,
        external_id=f"issue-{uuid.uuid4().hex[:8]}",
        title="Boom",
        level="error",
    )
    db.add(issue)
    db.flush()
    return issue


def _pull_request(db, repo, number):
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=number,
        title=f"PR {number}",
        author="alice",
        state="open",
        pr_url=f"https://github.com/acme/app/pull/{number}",
        head_branch=f"feat/{number}",
        base_branch="main",
    )
    db.add(pr)
    db.flush()
    return pr


_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _execution(db, *, status, minutes, issues=(), pull_requests=(), workflow="fix"):
    ex = Execution(provider="github", workflow=workflow, status=status)
    ex.issues = list(issues)
    ex.pull_requests = list(pull_requests)
    ex.created_at = _T0 + timedelta(minutes=minutes)
    db.add(ex)
    db.flush()
    return ex


# ── Stats cache ────────────────────────────────────────────────────


def test_stats_are_served_from_cache_until_an_event_invalidates_them(
    auth_client, app, mock_auth, fake_redis
):
    with app.database.session() as db:
        ws_id = _setup_workspace_with_stats(db, mock_auth).id

    first = auth_client.get(f"/workspaces/{ws_id}/stats").json()
    assert first["issues"]["handled"] == 5

    with app.database.session() as db:
        repo = (
            db.query(Repository)
            .join(Organization, Organization.id == Repository.org_id)
            .filter(Organization.workspace_id == ws_id)
            .first()
        )
        issue = _issue(db, repo)
        issue.triage_result = "not_actionable"
        db.commit()

    assert auth_client.get(f"/workspaces/{ws_id}/stats").json() == first

    asyncio.run(invalidate_stats(str(ws_id)))

    assert auth_client.get(f"/workspaces/{ws_id}/stats").json()["issues"]["handled"] == 6


def test_cached_stats_still_require_membership(auth_client, app, mock_auth, fake_redis):
    with app.database.session() as db:
        ws, _ = _workspace(db, mock_auth.id, member=False)
        db.commit()
        ws_id = str(ws.id)

    asyncio.run(store_stats(ws_id, "0", {"total_issues": 1}))

    resp = auth_client.get(f"/workspaces/{ws_id}/stats")
    assert resp.status_code == 403


def test_stats_fall_back_to_the_database_when_redis_fails(auth_client, app, mock_auth):
    with app.database.session() as db:
        ws_id = _setup_workspace_with_stats(db, mock_auth).id

    with patch("api.sse.stats_cache._redis", side_effect=RuntimeError("redis down")):
        resp = auth_client.get(f"/workspaces/{ws_id}/stats")

    assert resp.status_code == 200
    assert resp.json()["issues"]["handled"] == 5


async def test_stats_computed_before_an_event_are_not_served_after_it(fake_redis):
    _, version = await get_cached_stats("ws-1")
    await invalidate_stats("ws-1")
    await store_stats("ws-1", version, {"total_issues": 1})

    cached, _ = await get_cached_stats("ws-1")
    assert cached is None


async def test_publishing_invalidates_stats_before_the_event_goes_out():
    order: list[str] = []
    app = MagicMock()
    app.faststream.publish = AsyncMock(side_effect=lambda *_: order.append("publish"))

    with (
        patch("api.sse.publishers.get_current_app", return_value=app),
        patch(
            "api.sse.publishers.invalidate_stats",
            new=AsyncMock(side_effect=lambda ws: order.append(f"invalidate {ws}")),
        ),
    ):
        await publish_execution_event("ws-1", "updated", {})

    assert order == ["invalidate ws-1", "publish"]


# ── Executions ─────────────────────────────────────────────────────


def _executions_fixture(app, user_id):
    with app.database.session() as db:
        ws, repo = _workspace(db, user_id)
        other_ws, other_repo = _workspace(db, user_id)

        running_old = _execution(db, status="running", minutes=0, issues=[_issue(db, repo)])
        # A batch linking two issues must still show up once.
        running_batch = _execution(
            db, status="running", minutes=1, issues=[_issue(db, repo), _issue(db, repo)]
        )
        queued_review = _execution(
            db,
            status="queued",
            minutes=2,
            pull_requests=[_pull_request(db, repo, 7)],
            workflow="review",
        )
        completed = _execution(db, status="completed", minutes=3, issues=[_issue(db, repo)])
        _execution(db, status="running", minutes=4, issues=[_issue(db, other_repo)])
        db.commit()
        return {
            "ws": str(ws.id),
            "other_ws": str(other_ws.id),
            "running_old": str(running_old.id),
            "running_batch": str(running_batch.id),
            "queued_review": str(queued_review.id),
            "completed": str(completed.id),
        }


def test_active_executions_lists_running_then_queued_for_the_workspace_only(
    auth_client, app, mock_auth
):
    ids = _executions_fixture(app, mock_auth.id)

    resp = auth_client.get(f"/workspaces/{ids['ws']}/executions/active")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [
        ids["running_batch"],
        ids["running_old"],
        ids["queued_review"],
    ]
    review = items[2]
    assert review["kind"] == "pull_request"
    assert review["source"] == "github"
    assert review["pr_number"] == 7


def test_active_executions_caps_each_status(auth_client, app, mock_auth, monkeypatch):
    ids = _executions_fixture(app, mock_auth.id)
    monkeypatch.setitem(ACTIVE_EXECUTION_LIMITS, ExecutionStatus.RUNNING.value, 1)

    items = auth_client.get(f"/workspaces/{ids['ws']}/executions/active").json()["items"]

    assert [item["id"] for item in items] == [ids["running_batch"], ids["queued_review"]]


def test_active_executions_requires_membership(auth_client, app, mock_auth):
    with app.database.session() as db:
        ws, _ = _workspace(db, mock_auth.id, member=False)
        db.commit()
        ws_id = str(ws.id)

    assert auth_client.get(f"/workspaces/{ws_id}/executions/active").status_code == 403


def test_workspace_executions_scope_and_counts(auth_client, app, mock_auth):
    ids = _executions_fixture(app, mock_auth.id)

    everything = auth_client.get(f"/workspaces/{ids['ws']}/executions?limit=50").json()
    assert everything["total"] == 4
    assert [item["id"] for item in everything["items"]] == [
        ids["completed"],
        ids["queued_review"],
        ids["running_batch"],
        ids["running_old"],
    ]

    running = auth_client.get(f"/workspaces/{ids['ws']}/executions?status=running&limit=1").json()
    assert running["total"] == 2
    assert [item["id"] for item in running["items"]] == [ids["running_batch"]]
    assert running["has_more"] is True


def test_dashboard_reads_hold_one_connection(auth_client, app, mock_auth, fake_redis):
    ids = _executions_fixture(app, mock_auth.id)

    for path in ("stats", "stats", "executions/active"):
        peak = _peak_checked_out(
            app,
            lambda path=path: auth_client.get(f"/workspaces/{ids['ws']}/{path}").raise_for_status(),
        )
        assert peak == 1
