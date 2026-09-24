"""Tests for Sentry issue backfill consumer and endpoint."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.database import (
    db_create_issue,
    db_create_org,
    db_create_repository,
    db_create_workspace,
    db_get_issue_by_sentry_id,
    db_get_repositories_by_org,
    db_update_repository_mapping,
    db_upsert_repository,
)
from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.issues import TriageResult
from api.models.settings import BackfillScope
from api.models.users import User
from api.plugins.sentry.models import SentryIssue
from api.plugins.web.session import SessionData
from tests.utils.access import grant_org_access

# -- Fixtures ------------------------------------------------------------------


@pytest.fixture
def backfill_app(app):
    """App fixture with tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)
    yield app
    Base.metadata.drop_all(bind=db.engine)


class AuthUser:
    """Container for test user info (avoids detached session issues)."""

    def __init__(self, user_id, identity_id):
        self.id = user_id
        self.identity_id = identity_id


@pytest.fixture
def auth_user(backfill_app):
    """Create an authenticated user with a provider identity."""
    with backfill_app.database.session() as db:
        user = User(email="backfill@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="backfill-user-1",
            username="backfilluser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        db.refresh(user)
        db.refresh(identity)
        return AuthUser(user_id=user.id, identity_id=identity.id)


@pytest.fixture
def _mock_session(backfill_app, auth_user):
    """Mock the session so any cookie-bearing request is authenticated."""
    data = SessionData(
        user_id=str(auth_user.id),
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    sessions = backfill_app.web.sessions

    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def backfill_client(backfill_app, _mock_session):
    """Test client with authentication."""
    return TestClient(
        backfill_app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session"},
    )


@pytest.fixture
def workspace(backfill_app):
    """Create a test workspace."""
    with backfill_app.database.session() as db:
        return db_create_workspace(db=db, name="backfill-ws", slug="backfill-ws")


@pytest.fixture
def sentry_org(backfill_app, workspace, auth_user):
    """Create a test Sentry org linked to workspace with membership."""
    with backfill_app.database.session() as db:
        org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="backfill-sentry-org",
            external_org_id="backfill-org",
            provider="sentry",
            installation_id="sentry-install-backfill",
            settings={"backfill": "30d"},
        )
        grant_org_access(db, org, auth_user.id)
        return org.id


@pytest.fixture
def mapped_project(backfill_app, sentry_org, workspace):
    """Create a SourceProject with a repo mapping."""
    with backfill_app.database.session() as db:
        git_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="test-git-org",
            external_org_id="ext-backfill",
            installation_id="gh-backfill",
            provider="github",
            base_url="https://github.com",
        )
        repo = db_create_repository(
            db=db,
            org_id=git_org.id,
            name="my-repo",
            external_id="repo-1",
            web_url="https://github.com/org/my-repo",
            provider="github",
            provider_url="https://github.com/org/my-repo",
        )
        project = db_upsert_repository(
            db=db,
            org_id=sentry_org,
            external_id="500",
            name="my-project",
            provider="sentry",
        )
        db_update_repository_mapping(db, project.id, repo.id, "code_mapping")
        db.refresh(project)
        return project.id


def _make_sentry_issue(
    issue_id: str,
    title: str = "Test Issue",
    last_seen: datetime | None = None,
) -> SentryIssue:
    """Create a mock SentryIssue from the Sentry API."""
    return SentryIssue(
        id=issue_id,
        title=title,
        culprit="module.function",
        level="error",
        status="unresolved",
        firstSeen=datetime.now(UTC) - timedelta(days=10),
        lastSeen=last_seen or datetime.now(UTC) - timedelta(days=1),
        count="5",
    )


# -- Consumer tests ------------------------------------------------------------


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_creates_issues(
    mock_get_app, mock_sse, backfill_app, sentry_org, mapped_project
):
    """Consumer creates Issue records from Sentry API response."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[
            _make_sentry_issue("1001", "TypeError in handler"),
            _make_sentry_issue("1002", "ValueError in parser"),
        ]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(sentry_org), org_slug="backfill-org"))

    with backfill_app.database.session() as db:
        issue1 = db_get_issue_by_sentry_id(db, mapped_project, "1001")
        issue2 = db_get_issue_by_sentry_id(db, mapped_project, "1002")
        assert issue1 is not None
        assert issue1.title == "TypeError in handler"
        assert issue1.triage_result == TriageResult.PENDING
        assert issue2 is not None
        assert issue2.title == "ValueError in parser"

    backfill_app.sentry.list_issues.assert_called_once_with(
        "backfill-org",
        "500",
        query="is:unresolved",
        auth_token=None,
        base_url=None,
    )
    mock_sse.assert_awaited_once()
    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 2
    assert payload["skipped"] == 0


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_skips_existing_issues(
    mock_get_app, mock_sse, backfill_app, sentry_org, mapped_project
):
    """Consumer skips issues that already exist in the database."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    # Pre-create an issue
    with backfill_app.database.session() as db:
        db_create_issue(
            db=db,
            repository_id=mapped_project,
            external_id="1001",
            title="Existing issue",
            level="error",
            triage_result=TriageResult.ACTIONABLE.value,
        )

    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[
            _make_sentry_issue("1001", "Existing issue"),
            _make_sentry_issue("1003", "New issue"),
        ]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(sentry_org), org_slug="backfill-org"))

    with backfill_app.database.session() as db:
        existing = db_get_issue_by_sentry_id(db, mapped_project, "1001")
        assert existing is not None
        assert existing.title == "Existing issue"  # unchanged

        new = db_get_issue_by_sentry_id(db, mapped_project, "1003")
        assert new is not None
        assert new.title == "New issue"

    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 1
    assert payload["skipped"] == 1


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_filters_old_issues(
    mock_get_app, mock_sse, backfill_app, sentry_org, mapped_project
):
    """Consumer filters out issues not seen in the last 30 days."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[
            _make_sentry_issue(
                "2001", "Recent issue", last_seen=datetime.now(UTC) - timedelta(days=5)
            ),
            _make_sentry_issue(
                "2002", "Old issue", last_seen=datetime.now(UTC) - timedelta(days=60)
            ),
        ]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(sentry_org), org_slug="backfill-org"))

    with backfill_app.database.session() as db:
        recent = db_get_issue_by_sentry_id(db, mapped_project, "2001")
        old = db_get_issue_by_sentry_id(db, mapped_project, "2002")
        assert recent is not None
        assert old is None

    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 1


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_continues_on_api_error(
    mock_get_app, mock_sse, backfill_app, sentry_org, mapped_project
):
    """Consumer continues with other projects when one project's API call fails."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(side_effect=Exception("Sentry API error"))

    await backfill_issues(BackfillIssuesMessage(org_id=str(sentry_org), org_slug="backfill-org"))

    # Should still complete and publish SSE event
    mock_sse.assert_awaited_once()
    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 0


# -- Endpoint tests ------------------------------------------------------------


@patch("api.routers.sources.sentry.backfill.route.get_faststream_broker")
def test_trigger_backfill_queues_job(mock_broker, backfill_client, backfill_app, sentry_org):
    """POST /sources/sentry/backfill queues a backfill job."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance

    response = backfill_client.post(
        "/sources/sentry/backfill",
        params={"org_id": str(sentry_org)},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    mock_broker_instance.publish.assert_awaited_once()
    call_args = mock_broker_instance.publish.call_args
    msg = call_args.args[0]
    assert msg.org_id == str(sentry_org)
    assert msg.org_slug == "backfill-org"
    assert call_args.kwargs["stream"] == "jeanclode.events.sentry.backfill"


@patch("api.routers.sources.sentry.backfill.route.get_faststream_broker")
def test_trigger_backfill_no_access(mock_broker, backfill_client, backfill_app):
    """POST /sources/sentry/backfill returns 403 for unknown/unlinked org."""
    mock_broker.return_value = AsyncMock()

    response = backfill_client.post(
        "/sources/sentry/backfill",
        params={"org_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert response.status_code == 403


# -- Mapping consumer chain test -----------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_mapping_resolution_does_not_queue_backfill(mock_get_app, backfill_app):
    """Mapping resolution no longer chains a backfill.

    Project sync already queues one backfill per connect, and issues are
    stored by Sentry project rather than by repo (ADR-005), so a second run
    after mapping resolution only re-walked the Sentry API for issues that
    were already imported.
    """
    from api.routers.sources.sentry.projects import consumer
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    with backfill_app.database.session() as db:
        ws = db_create_workspace(db=db, name="chain-ws", slug="chain-ws")
        sentry_org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="chain-org",
            external_org_id="chain-org",
            provider="sentry",
            installation_id="install-chain-backfill",
        )
        git_org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="chain-git-org",
            external_org_id="ext-chain",
            installation_id="gh-chain",
            provider="github",
            base_url="https://github.com",
            # Resolution only considers orgs it can authenticate with
            auth_token_encrypted="encrypted-chain-token",
        )
        # A repo whose name matches the project slug, so fuzzy resolution
        # maps it — the condition that used to chain a backfill.
        db_create_repository(
            db=db,
            org_id=git_org.id,
            name="chain-project",
            external_id="repo-chain",
            web_url="https://github.com/org/chain-project",
            provider="github",
            provider_url="https://github.com/org/chain-project",
        )
        db_upsert_repository(
            db=db,
            org_id=sentry_org.id,
            external_id="600",
            name="chain-project",
            provider="sentry",
        )
        sentry_org_id = str(sentry_org.id)
        sentry_org_pk = sentry_org.id

    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=sentry_org_id))

    # The project was mapped — the condition that used to chain a backfill...
    with backfill_app.database.session() as db:
        projects = db_get_repositories_by_org(db, sentry_org_pk)
        assert any(p.mapped_repo_id is not None for p in projects)

    # ...and the consumer holds no broker to queue one with.
    assert not hasattr(consumer, "get_faststream_broker")


# -- Backfill scope tests ------------------------------------------------------


@pytest.fixture
def scoped_org(backfill_app, workspace, auth_user):
    """Factory for a Sentry org with a given backfill scope stored in settings."""

    def _make(scope: str | None, slug: str = "scoped-org"):
        with backfill_app.database.session() as db:
            org = db_create_org(
                db=db,
                workspace_id=workspace.id,
                name=slug,
                external_org_id=slug,
                provider="sentry",
                installation_id=f"install-{slug}",
                settings={} if scope is None else {"backfill": scope},
            )
            grant_org_access(db, org, auth_user.id)
            db_upsert_repository(
                db=db,
                org_id=org.id,
                external_id="700",
                name="scoped-project",
                provider="sentry",
            )
            db.commit()
            return org.id

    return _make


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_skipped_when_scope_is_none(
    mock_get_app, mock_sse, backfill_app, scoped_org
):
    """An org that opted out of backfill imports nothing and never calls Sentry."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    org_id = scoped_org("none", "opted-out")
    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(return_value=[_make_sentry_issue("2001")])

    await backfill_issues(BackfillIssuesMessage(org_id=str(org_id), org_slug="opted-out"))

    backfill_app.sentry.list_issues.assert_not_awaited()
    mock_sse.assert_awaited_once()
    assert mock_sse.call_args.kwargs["action"] == "skipped"


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_scope_all_imports_old_issues(
    mock_get_app, mock_sse, backfill_app, scoped_org
):
    """Scope 'all' drops the lookback cutoff entirely."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    org_id = scoped_org("all", "import-all")
    mock_get_app.return_value = backfill_app
    old = datetime.now(UTC) - timedelta(days=400)
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[_make_sentry_issue("2101", "Ancient error", last_seen=old)]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(org_id), org_slug="import-all"))

    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 1
    assert payload["scope"] == "all"


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_scope_7d_narrows_window(mock_get_app, mock_sse, backfill_app, scoped_org):
    """Scope '7d' imports the last week and leaves older issues out."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    org_id = scoped_org("7d", "last-week")
    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[
            _make_sentry_issue("2201", "Recent", last_seen=datetime.now(UTC) - timedelta(days=2)),
            _make_sentry_issue("2202", "Older", last_seen=datetime.now(UTC) - timedelta(days=20)),
        ]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(org_id), org_slug="last-week"))

    payload = mock_sse.call_args.kwargs["payload"]
    assert payload["created"] == 1
    assert payload["scope"] == "7d"


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_backfill_imports_nothing_without_stored_scope(
    mock_get_app, mock_sse, backfill_app, scoped_org
):
    """An org that never picked a scope imports nothing rather than a guessed window."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    org_id = scoped_org(None, "legacy-org")
    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(
        return_value=[
            _make_sentry_issue("2301", "Recent", last_seen=datetime.now(UTC) - timedelta(days=2)),
            _make_sentry_issue("2302", "Older", last_seen=datetime.now(UTC) - timedelta(days=90)),
        ]
    )

    await backfill_issues(BackfillIssuesMessage(org_id=str(org_id), org_slug="legacy-org"))

    backfill_app.sentry.list_issues.assert_not_awaited()
    assert mock_sse.call_args.kwargs["action"] == "skipped"


@pytest.mark.asyncio
@patch(
    "api.routers.sources.sentry.backfill.consumer.publish_backfill_event", new_callable=AsyncMock
)
@patch("api.routers.sources.sentry.backfill.consumer.get_current_app")
async def test_explicit_scope_overrides_opted_out_org(
    mock_get_app, mock_sse, backfill_app, scoped_org
):
    """A manual run carries its own scope and proceeds despite scope=none."""
    from api.routers.sources.sentry.backfill.consumer import backfill_issues
    from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage

    org_id = scoped_org("none", "manual-override")
    mock_get_app.return_value = backfill_app
    backfill_app.sentry.list_issues = AsyncMock(return_value=[_make_sentry_issue("2401")])

    await backfill_issues(
        BackfillIssuesMessage(
            org_id=str(org_id), org_slug="manual-override", scope=BackfillScope.DAYS_30
        )
    )

    backfill_app.sentry.list_issues.assert_awaited_once()
    assert mock_sse.call_args.kwargs["payload"]["created"] == 1


# -- Manual trigger scope resolution -------------------------------------------


@patch("api.routers.sources.sentry.backfill.route.get_faststream_broker")
def test_trigger_backfill_falls_back_for_opted_out_org(
    mock_broker, backfill_client, backfill_app, scoped_org
):
    """ "Import now" on an opted-out org still imports — skipping is a deferral."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = scoped_org("none", "trigger-opted-out")

    response = backfill_client.post(
        "/sources/sentry/backfill",
        params={"org_id": str(org_id)},
    )

    assert response.status_code == 202
    assert response.json()["scope"] == "30d"
    assert mock_broker_instance.publish.call_args.args[0].scope == BackfillScope.DAYS_30


@patch("api.routers.sources.sentry.backfill.route.get_faststream_broker")
def test_trigger_backfill_uses_org_scope_by_default(
    mock_broker, backfill_client, backfill_app, scoped_org
):
    """Without an explicit scope the org's configured window applies."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = scoped_org("7d", "trigger-week")

    response = backfill_client.post(
        "/sources/sentry/backfill",
        params={"org_id": str(org_id)},
    )

    assert response.status_code == 202
    assert response.json()["scope"] == "7d"


@patch("api.routers.sources.sentry.backfill.route.get_faststream_broker")
def test_trigger_backfill_honours_explicit_scope(
    mock_broker, backfill_client, backfill_app, scoped_org
):
    """An explicit scope in the request body wins over the org setting."""
    mock_broker_instance = AsyncMock()
    mock_broker.return_value = mock_broker_instance
    org_id = scoped_org("7d", "trigger-explicit")

    response = backfill_client.post(
        "/sources/sentry/backfill",
        params={"org_id": str(org_id)},
        json={"scope": "all"},
    )

    assert response.status_code == 202
    assert response.json()["scope"] == "all"
    assert mock_broker_instance.publish.call_args.args[0].scope == BackfillScope.ALL


# -- Settings merge ------------------------------------------------------------


def test_settings_patch_preserves_sibling_keys(backfill_client, backfill_app, scoped_org):
    """Changing the triage trigger must not reset the backfill scope."""
    org_id = scoped_org("none", "merge-org")

    response = backfill_client.patch(
        f"/organizations/{org_id}/settings",
        json={"triggers": {"triage": "manual"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["triggers"]["triage"] == "manual"
    assert body["backfill"] == "none"


def test_settings_patch_updates_backfill_alone(backfill_client, backfill_app, scoped_org):
    """A backfill-only PATCH is routed to the Sentry settings model."""
    org_id = scoped_org("30d", "backfill-only-org")

    response = backfill_client.patch(
        f"/organizations/{org_id}/settings",
        json={"backfill": "none"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["backfill"] == "none"
    assert body["triggers"]["triage"] == "manual"
