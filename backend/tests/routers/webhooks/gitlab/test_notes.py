"""End-to-end tests for the @jeanclode-bot mention webhook path on GitLab.

note events → mention detection → permission check → execution row +
dispatch event.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace
from api.database.execution import db_create_execution
from api.database.repository import db_get_repository_by_external_id
from api.models.executions import Execution, ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.identities import ProviderIdentity
from api.models.issues import Issue
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.routers.webhooks.gitlab.notes import has_mention


def _make_org_repo(db, *, settings, external_id: str):
    ws = db_create_workspace(db=db, name="test-ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    org = Organization(
        workspace_id=ws.id,
        name="acme",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        settings=settings,
    )
    db.add(org)
    db.flush()
    repo = Repository(
        org_id=org.id,
        name="acme-app",
        external_id=external_id,
        provider="gitlab",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return ws, org, repo


@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    with patch("api.routers.webhooks.gitlab.notes.get_faststream_broker", return_value=broker):
        yield broker


@pytest.fixture(autouse=True)
def _silence_perm_check():
    with patch(
        "api.routers.webhooks.gitlab.notes.fetch_member_access_level",
        new=AsyncMock(return_value=30),
    ):
        yield


def _seed_mr(db, repo: Repository, *, iid: int = 7, author: str = "alice") -> PullRequest:
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=iid,
        title="Add foo",
        author=author,
        state="open",
        pr_url=f"https://gitlab.com/{repo.name}/-/merge_requests/{iid}",
        head_branch="feat/foo",
        base_branch="main",
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr


# Respond has no trigger-mode setting — it always fires on a mention. An
# empty dict stands in for "whatever an org's settings normally look like".
_SETTINGS_ON: dict = {}


def test_has_mention_word_boundary():
    assert has_mention("Hi @jeanclode-bot")
    assert not has_mention("@jeanclode-botbot")
    assert not has_mention("hi @jeanclode-bot-fan")
    assert not has_mention("")


def _mr_top_level_payload(*, repo: Repository, iid: int, body: str, sender: str) -> dict:
    return {
        "object_kind": "note",
        "object_attributes": {
            "id": 1001,
            "note": body,
            "noteable_type": "MergeRequest",
            "url": f"https://gitlab.com/{repo.name}/-/merge_requests/{iid}#note_1001",
            "discussion_id": "disc-abc",
            "action": "create",
        },
        "merge_request": {
            "iid": iid,
            "id": 99001,
            "title": "Add foo",
            "author_id": 77,
            "state": "opened",
            "url": f"https://gitlab.com/{repo.name}/-/merge_requests/{iid}",
            "source_branch": "feat/foo",
            "target_branch": "main",
            "last_commit": {"id": "abc"},
        },
        "project": {"id": int(repo.external_id), "path_with_namespace": f"acme/{repo.name}"},
        "user": {"username": sender, "id": 4242},
    }


@pytest.mark.asyncio
async def test_mr_top_level_mention_dispatches(app, db_session, mock_broker):
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3001")
        _seed_mr(db, repo)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3001")
        result = await handle_note_event(
            _mr_top_level_payload(repo=repo, iid=7, body="@jeanclode-bot help here", sender="bob"),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1
    payload = mock_broker.publish.await_args.args[0]
    assert payload["workflow"] == ExecutionWorkflow.RESPOND.value
    # The CLI derives surface from the note URL — backend passes only target_url.
    assert "respond" not in payload
    assert "#note_1001" in payload["target_url"]

    with app.database.session() as db:
        assert db.query(Execution).count() == 1


@pytest.mark.asyncio
async def test_bot_top_level_mention_accepted(app, db_session, mock_broker):
    """A bot's top-level MR note is the review workflow's follow-up nudge."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3003")
        _seed_mr(db, repo, iid=9, author="acme_group_bot")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3003")
        result = await handle_note_event(
            _mr_top_level_payload(
                repo=repo,
                iid=9,
                body="@jeanclode-bot handle all the comments above",
                sender="acme_group_bot",
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_bot_mention_on_human_authored_mr_accepted(app, db_session, mock_broker):
    """Bot senders aren't gated on MR authorship — any bot note dispatches."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3004")
        _seed_mr(db, repo, iid=10, author="alice")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3004")
        result = await handle_note_event(
            _mr_top_level_payload(
                repo=repo,
                iid=10,
                body="@jeanclode-bot handle all the comments above",
                sender="acme_group_bot",
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_mr_inline_thread_mention_dispatches(app, db_session, mock_broker):
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3002")
        _seed_mr(db, repo, iid=8)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3002")
        payload = _mr_top_level_payload(
            repo=repo, iid=8, body="@jeanclode-bot please clarify", sender="bob"
        )
        # Add inline-position marker — switches surface to inline thread.
        payload["object_attributes"]["position"] = {"new_path": "src/foo.py", "new_line": 10}
        result = await handle_note_event(payload)
    assert result.processed is True
    dispatch = mock_broker.publish.await_args.args[0]
    # GitLab dispatch carries only the note URL — surface (inline vs
    # top-level) is determined by the CLI re-fetching the note.
    assert "respond" not in dispatch
    assert "#note_1001" in dispatch["target_url"]


@pytest.mark.asyncio
async def test_unauthorized_reporter_rejected(app, db_session, mock_broker):
    from api.routers.webhooks.gitlab import notes

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3003")
        _seed_mr(db, repo, iid=9)

    with (
        patch(
            "api.routers.webhooks.gitlab.notes.fetch_member_access_level",
            new=AsyncMock(return_value=20),  # Reporter — below Developer (30)
        ),
        app.database.session() as db,
    ):
        repo = db_get_repository_by_external_id(db, "3003")
        result = await notes.handle_note_event(
            _mr_top_level_payload(repo=repo, iid=9, body="@jeanclode-bot", sender="reporter"),
        )
    assert result.processed is False
    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_anyone_trigger_permission_bypasses_authorization_check(app, db_session, mock_broker):
    """``trigger_permission: anyone`` skips the access-level check entirely —
    a Reporter (below Developer) still dispatches."""
    from api.routers.webhooks.gitlab import notes

    with app.database.session() as db:
        _, _, repo = _make_org_repo(
            db, settings={"trigger_permission": "anyone"}, external_id="3009"
        )
        _seed_mr(db, repo, iid=9)

    with (
        patch(
            "api.routers.webhooks.gitlab.notes.fetch_member_access_level",
            new=AsyncMock(return_value=20),  # Reporter — below Developer (30)
        ),
        app.database.session() as db,
    ):
        repo = db_get_repository_by_external_id(db, "3009")
        result = await notes.handle_note_event(
            _mr_top_level_payload(repo=repo, iid=9, body="@jeanclode-bot", sender="reporter"),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_no_mention_no_dispatch(app, db_session, mock_broker):
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3004")
        _seed_mr(db, repo, iid=10)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3004")
        result = await handle_note_event(
            _mr_top_level_payload(repo=repo, iid=10, body="hello", sender="bob"),
        )
    assert result.processed is False
    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_resolve_action_skipped(app, db_session, mock_broker):
    """Notes emitted on resolve/unresolve actions are not dispatched."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3005")
        _seed_mr(db, repo, iid=11)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3005")
        payload = _mr_top_level_payload(repo=repo, iid=11, body="@jeanclode-bot", sender="bob")
        payload["object_attributes"]["action"] = "resolve"
        result = await handle_note_event(payload)
    assert result.processed is False
    assert mock_broker.publish.await_count == 0


def _issue_note_payload(
    *, repo: Repository, issue_iid: int, body: str, sender: str, include_note_url: bool = True
) -> dict:
    note_id = 2001
    issue_url = f"https://gitlab.com/{repo.name}/-/issues/{issue_iid}"
    return {
        "object_kind": "note",
        "object_attributes": {
            "id": note_id,
            "note": body,
            "noteable_type": "Issue",
            # Some GitLab versions omit the URL or omit the fragment — both are tested.
            **({"url": f"{issue_url}#note_{note_id}"} if include_note_url else {}),
            "discussion_id": "disc-issue-xyz",
            "action": "create",
        },
        "issue": {
            "iid": issue_iid,
            "id": 55001,
            "title": "Setup relevant metrics",
            "author_id": 7,
            "author": {"username": "alice"},
            "state": "opened",
            "url": issue_url,
        },
        "project": {"id": int(repo.external_id), "path_with_namespace": f"acme/{repo.name}"},
        "user": {"username": sender, "id": 4242},
    }


@pytest.mark.asyncio
async def test_issue_mention_dispatches(app, db_session, mock_broker):
    """@jeanclode-bot mention on a GitLab issue queues a respond run."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3006")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3006")
        result = await handle_note_event(
            _issue_note_payload(
                repo=repo, issue_iid=13, body="@jeanclode-bot can you handle it", sender="bob"
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1
    payload = mock_broker.publish.await_args.args[0]
    assert payload["workflow"] == ExecutionWorkflow.RESPOND.value
    assert "#note_2001" in payload["target_url"]

    with app.database.session() as db:
        assert db.query(Execution).count() == 1


@pytest.mark.asyncio
async def test_issue_mention_without_note_url_still_dispatches(app, db_session, mock_broker):
    """target_url always carries #note_<id> even when object_attributes.url is absent."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3007")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3007")
        result = await handle_note_event(
            _issue_note_payload(
                repo=repo,
                issue_iid=14,
                body="@jeanclode-bot pls help",
                sender="bob",
                include_note_url=False,  # simulates GitLab omitting object_attributes.url
            ),
        )
    assert result.processed is True
    payload = mock_broker.publish.await_args.args[0]
    # Fragment must be reconstructed from note.id even without object_attributes.url
    assert "#note_2001" in payload["target_url"]


@pytest.mark.asyncio
async def test_second_mr_mention_queues_instead_of_dropping(app, db_session, mock_broker):
    """A note on an MR that already has an in-flight respond queues, it doesn't drop."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3008")
        _seed_mr(db, repo, iid=21)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3008")
        payload = _mr_top_level_payload(
            repo=repo, iid=21, body="@jeanclode-bot help here", sender="bob"
        )
        first = await handle_note_event(payload)
        second = await handle_note_event(payload)

    assert first.processed is True
    assert second.processed is True
    assert "queued behind" in second.message
    assert mock_broker.publish.await_count == 1

    with app.database.session() as db:
        execs = db.query(Execution).order_by(Execution.created_at).all()
        assert len(execs) == 2
        assert all(e.workflow == ExecutionWorkflow.RESPOND.value for e in execs)
        assert all(e.status == ExecutionStatus.QUEUED.value for e in execs)


@pytest.mark.asyncio
async def test_issue_mention_not_blocked_by_unrelated_workflow(app, db_session, mock_broker):
    """A running ISSUE_RESOLVE must not block a respond note on the same issue."""
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3009")
        issue = Issue(
            repository_id=repo.id,
            external_id="13",
            title="Setup relevant metrics",
            level="info",
            status="open",
            author="alice",
            issue_url=f"https://gitlab.com/{repo.name}/-/issues/13",
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)
        db_create_execution(
            db,
            provider="gitlab",
            issues=[issue],
            workflow=ExecutionWorkflow.ISSUE_RESOLVE.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.RUNNING.value,
        )

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3009")
        result = await handle_note_event(
            _issue_note_payload(
                repo=repo, issue_iid=13, body="@jeanclode-bot status?", sender="bob"
            ),
        )

    assert result.processed is True
    assert "queued behind" not in result.message
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("synced", [True, False])
async def test_mention_records_the_sender_identity_when_synced(
    app, db_session, mock_broker, synced
):
    from api.routers.webhooks.gitlab.notes import handle_note_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings={}, external_id="8899")
        _seed_mr(db, repo, iid=31)
        identity_id = None
        if synced:
            identity = ProviderIdentity(provider="gitlab", external_id="4242", username="bob")
            db.add(identity)
            db.commit()
            identity_id = identity.id

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "8899")
        result = await handle_note_event(
            _mr_top_level_payload(repo=repo, iid=31, body="@jeanclode-bot help", sender="bob"),
        )
    assert result.processed is True

    with app.database.session() as db:
        execution = db.query(Execution).one()
        assert execution.triggered_by_identity_id == identity_id
