"""End-to-end tests for the GitHub PR / GitLab MR webhook auto-trigger path.

Webhook → upsert PR → label-gated dispatch → publish ManualDispatchEvent
on the provider's stream + create the Execution row. There is no
per-org trigger setting any more: only the ``jeanclode:review`` /
``jeanclode:summary`` label being added ever fires a workflow.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace
from api.database.repository import db_get_repository_by_external_id
from api.models.executions import Execution, ExecutionTrigger, ExecutionWorkflow
from api.models.organizations import Organization
from api.models.repositories import Repository


def _make_org_repo(db, *, provider, external_id: str, repo_settings=None):
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
    repo = Repository(
        org_id=org.id,
        name="acme-app",
        external_id=external_id,
        provider=provider,
        settings=repo_settings or {},
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return ws, org, repo


@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    with patch("api.routers.webhooks.git_dispatch.get_faststream_broker", return_value=broker):
        yield broker


@pytest.fixture(autouse=True)
def _silence_sse():
    with (
        patch("api.routers.webhooks.git_dispatch.publish_pull_request_event", new=AsyncMock()),
        patch(
            "api.routers.webhooks.github.pull_requests.publish_pull_request_event",
            new=AsyncMock(),
        ),
        patch(
            "api.routers.webhooks.gitlab.merge_requests.publish_pull_request_event",
            new=AsyncMock(),
        ),
    ):
        yield


def _github_payload(*, action, repo, labels=(), label_added=None):
    pr = {
        "number": 7,
        "id": 99001,
        "title": "Add foo",
        "html_url": "https://github.com/acme/app/pull/7",
        "user": {"login": "alice"},
        "head": {"ref": "feat/foo", "sha": "abc"},
        "base": {"ref": "main"},
        "state": "open",
        "labels": [{"name": name} for name in labels],
    }
    payload: dict = {
        "action": action,
        "pull_request": pr,
        "repository": {"id": int(repo.external_id)},
    }
    if label_added is not None:
        payload["label"] = {"name": label_added}
    return payload


@pytest.mark.asyncio
async def test_github_pr_opened_with_label_already_present_does_not_fire(
    app, db_session, mock_broker
):
    """The review label sitting on the PR at creation doesn't dispatch —
    only adding it does."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="1234")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "1234")
        assert repo is not None
        await handle_pull_request_event(
            _github_payload(action="opened", repo=repo, labels=["jeanclode:review"]),
        )
    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_github_label_added_after_open_fires_review(app, db_session, mock_broker):
    """Adding jeanclode:review fires REVIEW dispatch."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="5678")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "5678")
        assert repo is not None
        await handle_pull_request_event(
            _github_payload(
                action="labeled",
                repo=repo,
                labels=["jeanclode:review"],
                label_added="jeanclode:review",
            ),
        )
    assert mock_broker.publish.await_count == 1
    call = mock_broker.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.github.manual_dispatch"
    assert call.args[0]["workflow"] == ExecutionWorkflow.REVIEW.value

    # Execution exists with AUTO trigger.
    with app.database.session() as db:
        execs = db.query(Execution).all()
        assert len(execs) == 1
        assert execs[0].trigger == ExecutionTrigger.AUTO.value
        assert execs[0].workflow == ExecutionWorkflow.REVIEW.value


@pytest.mark.asyncio
async def test_github_label_added_unrelated_does_not_fire(app, db_session, mock_broker):
    """An unrelated label doesn't dispatch anything."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="9999")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9999")
        assert repo is not None
        await handle_pull_request_event(
            _github_payload(
                action="labeled",
                repo=repo,
                labels=["bug"],
                label_added="bug",
            ),
        )
    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_gitlab_update_with_new_commits_does_not_fire(app, db_session, mock_broker):
    """GitLab `update` with no label change normalizes to synchronize, which
    never dispatches — only a label addition does."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="5000")

    payload = {
        "object_attributes": {
            "iid": 12,
            "id": 12000,
            "title": "Refactor",
            "url": "https://gitlab.com/acme/app/-/merge_requests/12",
            "source_branch": "feat/x",
            "target_branch": "main",
            "state": "opened",
            "action": "update",
            "last_commit": {"id": "deadbeef"},
        },
        "project": {"id": 5000},
        "user": {"username": "alice"},
        "labels": [],
    }
    with app.database.session() as db:
        await handle_merge_request_event(payload)
    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_gitlab_label_added_via_changes_block_fires(app, db_session, mock_broker):
    """GitLab `update` with a labels delta normalizes to labeled and fires."""
    from api.routers.webhooks.gitlab.merge_requests import handle_merge_request_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="7000")

    payload = {
        "object_attributes": {
            "iid": 22,
            "id": 22000,
            "title": "Refactor",
            "url": "https://gitlab.com/acme/app/-/merge_requests/22",
            "source_branch": "feat/y",
            "target_branch": "main",
            "state": "opened",
            "action": "update",
            "last_commit": {"id": "abc"},
        },
        "project": {"id": 7000},
        "user": {"username": "alice"},
        "labels": [{"title": "jeanclode:review"}],
        "changes": {
            "labels": {
                "previous": [],
                "current": [{"title": "jeanclode:review"}],
            }
        },
    }
    with app.database.session() as db:
        await handle_merge_request_event(payload)
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_disabled_repo_ignores_pr_triggers(app, db_session, mock_broker):
    """A disabled repo starts nothing — neither PR creation nor the review label."""
    from api.routers.webhooks.github.pull_requests import handle_pull_request_event

    with app.database.session() as db:
        _make_org_repo(
            db,
            provider="github",
            external_id="9201",
            repo_settings={"enabled": False},
        )

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9201")
        assert repo is not None
        await handle_pull_request_event(_github_payload(action="opened", repo=repo))
        await handle_pull_request_event(
            _github_payload(action="labeled", repo=repo, label_added="jeanclode:review"),
        )

    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Execution).count() == 0
