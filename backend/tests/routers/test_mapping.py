"""Tests for project-to-repo mapping resolution and manual override."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api.database import (
    db_create_org,
    db_create_repository,
    db_create_workspace,
    db_get_org_by_id,
    db_get_repositories_by_org,
    db_get_repository_by_id,
    db_update_org,
    db_update_repository_mapping,
    db_upsert_repository,
)
from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.organizations import OnboardingStep
from api.models.repositories import MappingMethod
from api.models.users import User
from api.plugins.sentry.models import SentryCodeMapping
from api.plugins.web.session import SessionData
from tests.utils.access import grant_org_access


class IdHolder(BaseModel):
    """Holds IDs from ORM objects to avoid detached session issues."""

    id: UUID


@pytest.fixture
def mapping_app(app):
    """App fixture with tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)
    yield app
    Base.metadata.drop_all(bind=db.engine)


@pytest.fixture
def auth_user(mapping_app):
    """Create an authenticated user with a provider identity."""
    with mapping_app.database.session() as db:
        user = User(email="mapping-test@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="github",
            external_id="mapping-auth-user",
            username="mappinguser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        return IdHolder(id=identity.id), str(user.id)


@pytest.fixture
def _mock_session(mapping_app, auth_user):
    """Mock session so requests are authenticated."""
    _, user_id = auth_user
    data = SessionData(
        user_id=user_id,
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    sessions = mapping_app.web.sessions
    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def mapping_client(mapping_app, _mock_session):
    """Test client with authentication."""
    return TestClient(
        mapping_app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session"},
    )


@pytest.fixture
def workspace(mapping_app):
    """Create a test workspace."""
    with mapping_app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-workspace", slug="test-workspace")
        return IdHolder(id=ws.id)


@pytest.fixture
def git_org(mapping_app, workspace, auth_user):
    """Create a test git org the auth user can reach."""
    _, user_id = auth_user
    with mapping_app.database.session() as db:
        org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="test-org",
            external_org_id="12345",
            installation_id="gh-install-1",
            provider="github",
            base_url="https://github.com",
            auth_token_encrypted="test-encrypted-token",
        )
        grant_org_access(db, org, user_id)
        return IdHolder(id=org.id)


@pytest.fixture
def sentry_org(mapping_app, workspace, auth_user):
    """Create a test Sentry org the auth user can reach."""
    _, user_id = auth_user
    with mapping_app.database.session() as db:
        so = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="my-sentry-org",
            external_org_id="my-sentry-org",
            provider="sentry",
            installation_id="sentry-install-1",
        )
        grant_org_access(db, so, user_id)
        return IdHolder(id=so.id)


@pytest.fixture
def repos(mapping_app, git_org):
    """Create test repositories."""
    with mapping_app.database.session() as db:
        frontend = db_create_repository(
            db=db,
            org_id=git_org.id,
            external_id="repo-1",
            name="frontend",
            web_url="https://github.com/test-org/frontend",
            provider="github",
            provider_url="https://github.com",
        )
        backend_api = db_create_repository(
            db=db,
            org_id=git_org.id,
            external_id="repo-2",
            name="backend-api",
            web_url="https://github.com/test-org/backend-api",
            provider="github",
            provider_url="https://github.com",
        )
        return [IdHolder(id=frontend.id), IdHolder(id=backend_api.id)]


@pytest.fixture
def sentry_projects(mapping_app, sentry_org):
    """Create test source projects."""
    with mapping_app.database.session() as db:
        p1 = db_upsert_repository(
            db=db,
            org_id=sentry_org.id,
            external_id="100",
            name="frontend",
            provider="sentry",
        )
        p2 = db_upsert_repository(
            db=db,
            org_id=sentry_org.id,
            external_id="101",
            name="backend-api",
            provider="sentry",
        )
        p3 = db_upsert_repository(
            db=db,
            org_id=sentry_org.id,
            external_id="102",
            name="unmatched-project",
            provider="sentry",
        )
        return [IdHolder(id=p1.id), IdHolder(id=p2.id), IdHolder(id=p3.id)]


# ── GET /sources/sentry/projects ────────────────────────────────────


def test_list_sentry_projects(mapping_client, mapping_app, sentry_org, sentry_projects):
    """GET /sources/sentry/projects returns synced projects."""
    response = mapping_client.get(
        "/sources/sentry/projects",
        params={"org_id": str(sentry_org.id)},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


# ── POST /sources/sentry/projects/resolve ───────────────────────────


@patch("api.routers.sources.sentry.projects.route.get_faststream_broker")
def test_resolve_mappings_queues_task(mock_broker, mapping_client, git_org, sentry_org):
    """POST /sources/sentry/projects/resolve returns 202 and queues a task per git org."""
    mock_broker.return_value = AsyncMock()

    response = mapping_client.post(
        "/sources/sentry/projects/resolve",
        params={"org_id": str(sentry_org.id)},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    mock_broker.return_value.publish.assert_called_once()


@patch("api.routers.sources.sentry.projects.route.get_faststream_broker")
def test_resolve_mappings_sentry_org_not_found(mock_broker, mapping_client):
    """POST /sources/sentry/projects/resolve with unknown org returns 403 (no membership)."""
    response = mapping_client.post(
        "/sources/sentry/projects/resolve",
        params={"org_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 403


# ── PATCH /sources/sentry/projects/{id} ─────────────────────────────


def test_manual_mapping_set(mapping_client, mapping_app, sentry_projects, repos):
    """PATCH /sources/sentry/projects/{id} sets manual mapping."""
    response = mapping_client.patch(
        f"/sources/sentry/projects/{sentry_projects[0].id}",
        json={"repo_id": str(repos[0].id)},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mapped_repo_id"] == str(repos[0].id)
    assert data["mapping_method"] == "manual"


def test_manual_mapping_clear(mapping_client, mapping_app, sentry_projects, repos):
    """PATCH /sources/sentry/projects/{id} with null clears mapping."""
    # First set a mapping
    with mapping_app.database.session() as db:
        db_update_repository_mapping(
            db, sentry_projects[0].id, repos[0].id, MappingMethod.FUZZY.value
        )

    # Clear it via API
    response = mapping_client.patch(
        f"/sources/sentry/projects/{sentry_projects[0].id}",
        json={"repo_id": None},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mapped_repo_id"] is None
    assert data["mapping_method"] == "manual"


def test_manual_mapping_not_found(mapping_client, mapping_app):
    """PATCH /sources/sentry/projects/{id} for unknown project returns 404."""
    response = mapping_client.patch(
        "/sources/sentry/projects/00000000-0000-0000-0000-000000000000",
        json={"repo_id": None},
    )
    assert response.status_code == 404


# ── Consumer: resolve_project_mappings ──────────────────────────────


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_code_mapping_resolution(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Consumer resolves projects via code mappings."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(
        return_value=[
            SentryCodeMapping(
                id="cm-1",
                projectSlug="frontend",
                repoName="test-org/frontend",
            ),
        ]
    )

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        projects = db_get_repositories_by_org(db, sentry_org.id)
        mapped = {p.name: p for p in projects}

        # frontend matched via code mapping
        assert mapped["frontend"].mapped_repo_id == repos[0].id
        assert mapped["frontend"].mapping_method == MappingMethod.CODE_MAPPING.value

        # backend-api matched via fuzzy name
        assert mapped["backend-api"].mapped_repo_id == repos[1].id
        assert mapped["backend-api"].mapping_method == MappingMethod.FUZZY.value

        # unmatched-project remains unmapped
        assert mapped["unmatched-project"].mapped_repo_id is None


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_fuzzy_name_matching(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Consumer falls back to fuzzy name matching when no code mappings."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        projects = db_get_repositories_by_org(db, sentry_org.id)
        mapped = {p.name: p for p in projects}

        assert mapped["frontend"].mapped_repo_id == repos[0].id
        assert mapped["frontend"].mapping_method == MappingMethod.FUZZY.value

        assert mapped["backend-api"].mapped_repo_id == repos[1].id
        assert mapped["backend-api"].mapping_method == MappingMethod.FUZZY.value

        assert mapped["unmatched-project"].mapped_repo_id is None


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_fuzzy_matches_namespaced_repo_name(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org
):
    """Fuzzy matching still works when Repository.name is a namespaced GitLab
    path (group/repo, per the bulk sync path in gitlab/route.py) and the
    Sentry project slug is the bare project name."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    with mapping_app.database.session() as db:
        repo = db_create_repository(
            db=db,
            org_id=git_org.id,
            external_id="repo-namespaced",
            name="jdoe/reports-api",
            web_url="https://gitlab.example.com/jdoe/reports-api",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )
        repo_id = repo.id
        project = db_upsert_repository(
            db=db,
            org_id=sentry_org.id,
            external_id="200",
            name="reports-api",
            provider="sentry",
        )
        project_id = project.id

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        mapped_project = db_get_repository_by_id(db, project_id)
        assert mapped_project.mapped_repo_id == repo_id
        assert mapped_project.mapping_method == MappingMethod.FUZZY.value


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_preserves_manual_mappings(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Consumer skips projects with manual mapping_method."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    # Manually map "frontend" to backend-api repo
    with mapping_app.database.session() as db:
        db_update_repository_mapping(
            db, sentry_projects[0].id, repos[1].id, MappingMethod.MANUAL.value
        )

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        project = db_get_repository_by_id(db, sentry_projects[0].id)
        # Manual mapping preserved — still points to repos[1], not repos[0]
        assert project.mapped_repo_id == repos[1].id
        assert project.mapping_method == MappingMethod.MANUAL.value


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_updates_onboarding_step(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Consumer updates git org onboarding_step to 'mapping'."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        org = db_get_org_by_id(db, git_org.id)
        assert org.onboarding_step == OnboardingStep.MAPPING.value


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_handles_sentry_api_failure(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Consumer still does fuzzy matching when Sentry code mappings API fails."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(side_effect=Exception("API error"))

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        projects = db_get_repositories_by_org(db, sentry_org.id)
        mapped = {p.name: p for p in projects}
        # Fuzzy matching still works
        assert mapped["frontend"].mapped_repo_id == repos[0].id
        assert mapped["frontend"].mapping_method == MappingMethod.FUZZY.value


@pytest.mark.asyncio
@patch("api.routers.sources.sentry.projects.consumer.publish_mapping_event", new_callable=AsyncMock)
@patch("api.routers.sources.sentry.projects.consumer.get_current_app")
async def test_consumer_does_not_regress_complete_onboarding(
    mock_get_app, mock_publish, mapping_app, git_org, sentry_org, sentry_projects, repos
):
    """Re-resolving on a COMPLETE git org does not downgrade onboarding_step."""
    from api.routers.sources.sentry.projects.consumer import resolve_project_mappings
    from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage

    # Set org to complete
    with mapping_app.database.session() as db:
        db_update_org(db, git_org.id, onboarding_step=OnboardingStep.COMPLETE.value)

    mock_get_app.return_value = mapping_app
    mapping_app.sentry.list_code_mappings = AsyncMock(return_value=[])

    await resolve_project_mappings(ResolveMappingsMessage(sentry_org_id=str(sentry_org.id)))

    with mapping_app.database.session() as db:
        org = db_get_org_by_id(db, git_org.id)
        assert org.onboarding_step == OnboardingStep.COMPLETE.value


# ── PATCH /organizations/{id} ─────────────────────────────────────────────


def test_update_org_onboarding_step(mapping_client, mapping_app, git_org):
    """PATCH /organizations/{id} updates onboarding_step."""
    response = mapping_client.patch(
        f"/organizations/{git_org.id}",
        json={"onboarding_step": "complete"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(git_org.id)
    assert data["onboarding_step"] == "complete"


def test_update_org_not_found(mapping_client, mapping_app):
    """PATCH /organizations/{id} for unknown org returns 403 (no membership)."""
    response = mapping_client.patch(
        "/organizations/00000000-0000-0000-0000-000000000000",
        json={"onboarding_step": "complete"},
    )
    assert response.status_code == 403


def test_update_org_empty_body(mapping_client, mapping_app, git_org):
    """PATCH /organizations/{id} with empty body returns current state."""
    response = mapping_client.patch(
        f"/organizations/{git_org.id}",
        json={},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["onboarding_step"] == "git_provider"


# ── GET /organizations/{id} ──────────────────────────────────────────────


def test_get_org(mapping_client, git_org):
    """GET /organizations/{id} returns organization details."""
    response = mapping_client.get(f"/organizations/{git_org.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test-org"
    assert data["provider"] == "github"
    assert data["onboarding_step"] == "git_provider"


def test_get_org_not_found(mapping_client, mapping_app):
    """GET /organizations/{id} for unknown org returns 403 (no membership)."""
    response = mapping_client.get("/organizations/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 403
