"""End-to-end tests for the @jeanclode-bot mention webhook path on GitHub.

issue_comment / pull_request_review / pull_request_review_comment →
mention detection → permission check → execution row + dispatch event.
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
from api.routers.webhooks.github.mentions import has_mention


def _make_org_repo(db, *, settings, external_id: str):
    ws = db_create_workspace(db=db, name="test-ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    org = Organization(
        workspace_id=ws.id,
        name="acme",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider="github",
        settings=settings,
        installation_id=f"inst-{uuid.uuid4().hex[:6]}",
    )
    db.add(org)
    db.flush()
    repo = Repository(
        org_id=org.id,
        name="acme-app",
        external_id=external_id,
        provider="github",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return ws, org, repo


@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    with patch("api.routers.webhooks.github.mentions.get_faststream_broker", return_value=broker):
        yield broker


@pytest.fixture(autouse=True)
def _silence_perm_check():
    """Default: any non-bot user passes the perm check.

    Tests that exercise unauthorized paths override this with their own
    patch in the test body.
    """
    with patch(
        "api.routers.webhooks.github.mentions.fetch_collaborator_permission",
        new=AsyncMock(return_value="write"),
    ):
        yield


@pytest.fixture(autouse=True)
def _silence_install_token():
    with patch(
        "api.routers.webhooks.github.mentions.get_current_app",
    ) as gca:
        app = AsyncMock()
        app.github = AsyncMock()
        app.github.get_installation_access_token = AsyncMock(return_value="token")
        gca.return_value = app
        yield


def _seed_pr(db, repo: Repository, *, number: int = 7, author: str = "alice") -> PullRequest:
    pr = PullRequest(
        repository_id=repo.id,
        pr_number=number,
        title="Add foo",
        author=author,
        state="open",
        pr_url=f"https://github.com/{repo.name}/pull/{number}",
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
    assert has_mention("hey @jeanclode-bot tell me about this")
    assert has_mention("Maybe @jeanclode-bot could fix this?")
    assert has_mention("@jeanclode-bot")
    # Suffixed handles must NOT trigger — old @-name without the suffix
    # also doesn't trigger.
    assert not has_mention("emailed @jeanclode-botbot for help")
    assert not has_mention("hi @jeanclode-bot-fan")
    assert not has_mention("hey @jeanclode")  # the real user account
    assert not has_mention("no mention here")
    assert not has_mention("")


def _issue_comment_payload(
    *,
    repo: Repository,
    pr_number: int,
    body: str,
    sender: str,
    sender_type: str = "User",
    pr_author: str = "alice",
    sender_id: int | None = None,
) -> dict:
    return {
        "action": "created",
        "issue": {
            "number": pr_number,
            "title": "Add foo",
            "state": "open",
            "html_url": f"https://github.com/{repo.name}/pull/{pr_number}",
            "user": {"login": pr_author},
            "pull_request": {"url": "..."},
        },
        "comment": {
            "id": 1001,
            "body": body,
            "html_url": f"https://github.com/{repo.name}/pull/{pr_number}#issuecomment-1001",
        },
        "repository": {"id": int(repo.external_id), "full_name": f"acme/{repo.name}"},
        "sender": {"login": sender, "type": sender_type, "id": sender_id},
    }


@pytest.mark.asyncio
async def test_pr_top_level_mention_creates_execution_and_dispatches(app, db_session, mock_broker):
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="7777")
        _seed_pr(db, repo)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "7777")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo, pr_number=7, body="@jeanclode-bot help here", sender="bob"
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1
    call = mock_broker.publish.await_args
    assert call.kwargs["stream"] == "jeanclode.events.github.manual_dispatch"
    payload = call.args[0]
    assert payload["workflow"] == ExecutionWorkflow.RESPOND.value
    # Body / author / surface no longer ride the dispatch event — the CLI
    # fetches them from the comment URL fragment instead.
    assert "respond" not in payload
    assert payload["target_url"].startswith("https://github.com/")
    assert payload["pull_request_id"]
    assert payload["issue_id"] is None

    with app.database.session() as db:
        execs = db.query(Execution).all()
        assert len(execs) == 1
        assert execs[0].workflow == ExecutionWorkflow.RESPOND.value


@pytest.mark.asyncio
async def test_no_mention_no_dispatch(app, db_session, mock_broker):
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="2222")
        _seed_pr(db, repo)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "2222")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload(repo=repo, pr_number=7, body="just a comment", sender="bob"),
        )
    assert result.processed is False
    assert mock_broker.publish.await_count == 0


@pytest.mark.asyncio
async def test_unauthorized_commenter_rejected(app, db_session, mock_broker):
    from api.routers.webhooks.github import mentions

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="3333")
        _seed_pr(db, repo)

    with (
        patch(
            "api.routers.webhooks.github.mentions.fetch_collaborator_permission",
            new=AsyncMock(return_value="read"),
        ),
        app.database.session() as db,
    ):
        repo = db_get_repository_by_external_id(db, "3333")
        result = await mentions.handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo, pr_number=7, body="@jeanclode-bot push fix", sender="external"
            ),
        )
    assert result.processed is False
    assert mock_broker.publish.await_count == 0
    with app.database.session() as db:
        assert db.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_anyone_trigger_permission_bypasses_authorization_check(app, db_session, mock_broker):
    """``trigger_permission: anyone`` skips the collaborator-permission check
    entirely — a commenter with only read access still dispatches."""
    from api.routers.webhooks.github import mentions

    with app.database.session() as db:
        _, _, repo = _make_org_repo(
            db, settings={"trigger_permission": "anyone"}, external_id="4444"
        )
        _seed_pr(db, repo)

    with (
        patch(
            "api.routers.webhooks.github.mentions.fetch_collaborator_permission",
            new=AsyncMock(return_value="read"),
        ),
        app.database.session() as db,
    ):
        repo = db_get_repository_by_external_id(db, "4444")
        result = await mentions.handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo, pr_number=7, body="@jeanclode-bot push fix", sender="external"
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1
    with app.database.session() as db:
        assert db.query(Execution).count() == 1


@pytest.mark.asyncio
async def test_bot_mention_on_issue_comment_accepted(app, db_session, mock_broker):
    """Bot senders are accepted on every surface, including issue_comment."""
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="5555")
        _seed_pr(db, repo, author="jeanclode-bot[bot]")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "5555")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo,
                pr_number=7,
                body="@jeanclode-bot try again",
                sender="jeanclode-bot[bot]",
                sender_type="Bot",
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_bot_top_level_mention_on_human_pr_accepted(app, db_session, mock_broker):
    """Bot senders aren't gated on PR authorship either."""
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="5556")
        _seed_pr(db, repo, author="alice")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "5556")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo,
                pr_number=7,
                body="@jeanclode-bot handle all the comments above",
                sender="jeanclode-bot[bot]",
                sender_type="Bot",
                pr_author="alice",
            ),
        )
    assert result.processed is True
    assert mock_broker.publish.await_count == 1


@pytest.mark.asyncio
async def test_bot_mention_on_pr_review_submission_accepted(app, db_session, mock_broker):
    """A bot review submission carrying the mention dispatches."""
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="6666")
        _seed_pr(db, repo, number=8, author="jeanclode-bot[bot]")

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6666")
        payload = {
            "action": "submitted",
            "review": {
                "id": 5001,
                "body": "@jeanclode-bot fix unresolved comments",
                "html_url": f"https://github.com/{repo.name}/pull/8#pullrequestreview-5001",
                "submitted_at": "2026-01-01T00:00:00Z",
                "state": "commented",
            },
            "pull_request": {
                "number": 8,
                "id": 99002,
                "title": "Bot PR",
                "user": {"login": "jeanclode-bot[bot]"},
                "state": "open",
                "html_url": f"https://github.com/{repo.name}/pull/8",
                "head": {"ref": "fix/branch", "sha": "def"},
                "base": {"ref": "main"},
            },
            "repository": {"id": int(repo.external_id), "full_name": f"acme/{repo.name}"},
            "sender": {"login": "jeanclode-bot[bot]", "type": "Bot"},
        }
        result = await handle_mention_event("pull_request_review", payload)
    assert result.processed is True
    assert mock_broker.publish.await_count == 1
    dispatch = mock_broker.publish.await_args.args[0]
    # URL fragment encodes pullrequestreview surface; CLI parses it.
    assert "#pullrequestreview-5001" in dispatch["target_url"]


@pytest.mark.asyncio
async def test_inline_review_comment_mention_dispatches(app, db_session, mock_broker):
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="8888")
        _seed_pr(db, repo, number=9)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "8888")
        payload = {
            "action": "created",
            "comment": {
                "id": 7001,
                "body": "@jeanclode-bot please clarify",
                "html_url": f"https://github.com/{repo.name}/pull/9#discussion_r7001",
                "pull_request_review_id": 12345,
            },
            "pull_request": {
                "number": 9,
                "id": 99003,
                "title": "Add bar",
                "user": {"login": "alice"},
                "state": "open",
                "html_url": f"https://github.com/{repo.name}/pull/9",
                "head": {"ref": "feat/bar", "sha": "ghi"},
                "base": {"ref": "main"},
            },
            "repository": {"id": int(repo.external_id), "full_name": f"acme/{repo.name}"},
            "sender": {"login": "carol", "type": "User"},
        }
        result = await handle_mention_event("pull_request_review_comment", payload)
    assert result.processed is True
    dispatch = mock_broker.publish.await_args.args[0]
    # Inline-thread mentions ride a discussion_r-prefixed URL fragment.
    assert "#discussion_r" in dispatch["target_url"] or dispatch["target_url"].endswith(
        "discussion_r7001"
    )


def _issue_comment_payload_on_issue(
    *, repo: Repository, issue_number: int, body: str, sender: str
) -> dict:
    return {
        "action": "created",
        "issue": {
            "number": issue_number,
            "title": "Something broke",
            "state": "open",
            "html_url": f"https://github.com/{repo.name}/issues/{issue_number}",
            "user": {"login": "dave"},
            # No "pull_request" key — this is what marks it a standalone issue.
        },
        "comment": {
            "id": 2001,
            "body": body,
            "html_url": f"https://github.com/{repo.name}/issues/{issue_number}#issuecomment-2001",
        },
        "repository": {"id": int(repo.external_id), "full_name": f"acme/{repo.name}"},
        "sender": {"login": sender, "type": "User"},
    }


@pytest.mark.asyncio
async def test_second_pr_mention_queues_instead_of_dropping(app, db_session, mock_broker):
    """A mention on a PR that already has an in-flight respond queues, it doesn't drop."""
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="6001")
        _seed_pr(db, repo, number=11)

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6001")
        payload = _issue_comment_payload(
            repo=repo, pr_number=11, body="@jeanclode-bot help here", sender="bob"
        )
        first = await handle_mention_event("issue_comment", payload)
        second = await handle_mention_event("issue_comment", payload)

    assert first.processed is True
    assert second.processed is True
    assert "queued behind" in second.message
    # Only the first mention actually got published — the second is parked.
    assert mock_broker.publish.await_count == 1

    with app.database.session() as db:
        execs = db.query(Execution).order_by(Execution.created_at).all()
        assert len(execs) == 2
        assert all(e.workflow == ExecutionWorkflow.RESPOND.value for e in execs)
        assert all(e.status == ExecutionStatus.QUEUED.value for e in execs)


@pytest.mark.asyncio
async def test_issue_mention_not_blocked_by_unrelated_workflow(app, db_session, mock_broker):
    """A running ISSUE_RESOLVE must not block a respond mention on the same issue."""
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="6002")
        issue = Issue(
            repository_id=repo.id,
            external_id="42",
            title="Something broke",
            level="info",
            status="open",
            author="dave",
            issue_url=f"https://github.com/{repo.name}/issues/42",
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)
        db_create_execution(
            db,
            provider="github",
            issues=[issue],
            workflow=ExecutionWorkflow.ISSUE_RESOLVE.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.RUNNING.value,
        )

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "6002")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload_on_issue(
                repo=repo, issue_number=42, body="@jeanclode-bot status?", sender="bob"
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
    from api.routers.webhooks.github.mentions import handle_mention_event

    with app.database.session() as db:
        _, _, repo = _make_org_repo(db, settings=_SETTINGS_ON, external_id="7788")
        _seed_pr(db, repo)
        identity_id = None
        if synced:
            identity = ProviderIdentity(provider="github", external_id="555", username="bob")
            db.add(identity)
            db.commit()
            identity_id = identity.id

    with app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "7788")
        result = await handle_mention_event(
            "issue_comment",
            _issue_comment_payload(
                repo=repo, pr_number=7, body="@jeanclode-bot help", sender="bob", sender_id=555
            ),
        )
    assert result.processed is True

    with app.database.session() as db:
        execution = db.query(Execution).one()
        assert execution.triggered_by_identity_id == identity_id
