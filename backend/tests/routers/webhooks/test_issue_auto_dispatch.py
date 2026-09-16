"""End-to-end tests for the GitHub / GitLab issue webhook auto-trigger path.

Webhook → upsert Issue → label-gated dispatch → publish on the
provider's issue_resolve stream + create the Execution row. There is no
per-org trigger setting: only the ``jeanclode:resolve`` label being added
ever fires the workflow — an issue simply being opened never does.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace
from api.database.repository import db_get_repository_by_external_id
from api.models.executions import Execution, ExecutionTrigger, ExecutionWorkflow
from api.models.issues import Issue
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
        patch("api.routers.webhooks.github.issues.publish_issue_event", new=AsyncMock()),
        patch("api.routers.webhooks.gitlab.issues.publish_issue_event", new=AsyncMock()),
    ):
        yield


def _github_payload(*, action, repo, state="open", label_added=None, labels=()):
    payload: dict = {
        "action": action,
        "issue": {
            "number": 42,
            "title": "Crash on login",
            "state": state,
            "user": {"login": "alice"},
            "html_url": "https://github.com/acme/app/issues/42",
            "comments": 0,
            "labels": [{"name": name} for name in labels],
            "created_at": "2026-06-10T10:00:00Z",
            "updated_at": "2026-06-10T10:00:00Z",
        },
        "repository": {"id": int(repo.external_id)},
    }
    if label_added is not None:
        payload["label"] = {"name": label_added}
    return payload


def _gitlab_payload(*, action, repo, state="opened", label_added=None):
    payload: dict = {
        "object_kind": "issue",
        "object_attributes": {
            "iid": 42,
            "title": "Crash on login",
            "state": state,
            "action": action,
            "url": "https://gitlab.com/acme/app/-/issues/42",
            "created_at": "2026-06-10 10:00:00 UTC",
            "updated_at": "2026-06-10 10:00:00 UTC",
        },
        "project": {"id": int(repo.external_id)},
        "user": {"username": "alice"},
    }
    if label_added is not None:
        payload["changes"] = {
            "labels": {
                "previous": [],
                "current": [{"title": label_added}],
            }
        }
    return payload


@pytest.mark.asyncio
async def test_github_issue_opened_does_not_dispatch(app, db_session, mock_broker):
    """A new issue is ingested but never auto-dispatches on its own."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="2345")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "2345")
        assert repo is not None
        await handle_issue_event(_github_payload(action="opened", repo=repo))

    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Issue).count() == 1
        assert db.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_github_issue_opened_with_resolve_label_dispatches(app, db_session, mock_broker):
    """An issue created with the resolve label dispatches from the `opened` event itself."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="2346")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "2346")
        assert repo is not None
        await handle_issue_event(
            _github_payload(action="opened", repo=repo, labels=["jeanclode:resolve"]),
        )

    assert mock_broker.publish.await_count == 1
    assert (
        mock_broker.publish.await_args.kwargs["stream"] == "jeanclode.events.github.issue_resolve"
    )


@pytest.mark.asyncio
async def test_github_resolve_label_dispatches(app, db_session, mock_broker):
    """`jeanclode:resolve` label added: issue created in DB + workflow queued."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="3456")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3456")
        assert repo is not None
        await handle_issue_event(
            _github_payload(action="labeled", repo=repo, label_added="jeanclode:resolve"),
        )

    assert mock_broker.publish.await_count == 1
    call = mock_broker.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.github.issue_resolve"
    assert call.args[0]["workflow"] == ExecutionWorkflow.ISSUE_RESOLVE.value

    with app.database.session() as db:
        issue = db.query(Issue).one()
        assert issue.external_id == "42"
        execs = db.query(Execution).all()
        assert len(execs) == 1
        assert execs[0].workflow == ExecutionWorkflow.ISSUE_RESOLVE.value
        assert execs[0].trigger == ExecutionTrigger.AUTO.value
        assert execs[0].provider == "github"
        assert call.args[0]["issue_id"] == str(issue.id)
        assert call.args[0]["execution_id"] == str(execs[0].id)


@pytest.mark.asyncio
async def test_github_unrelated_label_no_dispatch(app, db_session, mock_broker):
    """An unrelated label never dispatches."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="4567")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "4567")
        assert repo is not None
        await handle_issue_event(
            _github_payload(action="labeled", repo=repo, label_added="bug"),
        )

    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_github_resolve_label_on_closed_issue_no_dispatch(app, db_session, mock_broker):
    """The resolve label on a closed issue does not dispatch."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="5678")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "5678")
        assert repo is not None
        await handle_issue_event(
            _github_payload(
                action="labeled", repo=repo, state="closed", label_added="jeanclode:resolve"
            ),
        )

    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_github_no_duplicate_when_active_execution(app, db_session, mock_broker):
    """A second trigger on an issue with an active execution is skipped."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="github", external_id="6789")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6789")
        assert repo is not None
        await handle_issue_event(
            _github_payload(action="labeled", repo=repo, label_added="jeanclode:resolve"),
        )
        await handle_issue_event(
            _github_payload(action="labeled", repo=repo, label_added="jeanclode:resolve"),
        )

    assert mock_broker.publish.await_count == 1
    with app.database.session() as db:
        assert db.query(Execution).count() == 1


@pytest.mark.asyncio
async def test_gitlab_issue_open_does_not_dispatch(app, db_session, mock_broker):
    """GitLab `open` action is ingested but never auto-dispatches on its own."""
    from api.routers.webhooks.gitlab.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="6000")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6000")
        assert repo is not None
        await handle_issue_event(_gitlab_payload(action="open", repo=repo))

    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Issue).count() == 1


@pytest.mark.asyncio
async def test_gitlab_issue_opened_with_resolve_label_dispatches(app, db_session, mock_broker):
    """GitLab sends no label update for labels set at creation, so `open` must fire on them."""
    from api.routers.webhooks.gitlab.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="6100")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6100")
        assert repo is not None
        payload = _gitlab_payload(action="open", repo=repo)
        payload["labels"] = [{"title": "jeanclode:resolve"}]
        await handle_issue_event(payload)

    assert mock_broker.publish.await_count == 1
    with app.database.session() as db:
        assert db.query(Execution).count() == 1


@pytest.mark.asyncio
async def test_gitlab_resolve_label_via_changes_block_dispatches(app, db_session, mock_broker):
    """GitLab `update` with a labels delta adding `jeanclode:resolve` dispatches."""
    from api.routers.webhooks.gitlab.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="7000")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "7000")
        assert repo is not None
        await handle_issue_event(
            _gitlab_payload(action="update", repo=repo, label_added="jeanclode:resolve"),
        )

    assert mock_broker.publish.await_count == 1
    assert (
        mock_broker.publish.await_args.kwargs["stream"] == "jeanclode.events.gitlab.issue_resolve"
    )

    with app.database.session() as db:
        execs = db.query(Execution).all()
        assert len(execs) == 1
        assert execs[0].provider == "gitlab"


@pytest.mark.asyncio
async def test_gitlab_unrelated_label_no_dispatch(app, db_session, mock_broker):
    """GitLab `update` adding an unrelated label does not dispatch."""
    from api.routers.webhooks.gitlab.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(db, provider="gitlab", external_id="8000")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "8000")
        assert repo is not None
        await handle_issue_event(
            _gitlab_payload(action="update", repo=repo, label_added="bug"),
        )

    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_disabled_repo_ignores_resolve_label(app, db_session, mock_broker):
    """A disabled repo is inert: the resolve label ingests the issue but starts nothing."""
    from api.routers.webhooks.github.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(
            db,
            provider="github",
            external_id="9101",
            repo_settings={"enabled": False},
        )

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9101")
        assert repo is not None
        await handle_issue_event(
            _github_payload(action="labeled", repo=repo, label_added="jeanclode:resolve"),
        )

    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Issue).count() == 1
        assert db.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_disabled_repo_ignores_issue_opened(app, db_session, mock_broker):
    """A disabled repo ignores issue-open events too (nothing dispatches from those anyway)."""
    from api.routers.webhooks.gitlab.issues import handle_issue_event

    with app.database.session() as db:
        _make_org_repo(
            db,
            provider="gitlab",
            external_id="9102",
            repo_settings={"enabled": False},
        )

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9102")
        assert repo is not None
        await handle_issue_event(_gitlab_payload(action="open", repo=repo))

    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Execution).count() == 0
