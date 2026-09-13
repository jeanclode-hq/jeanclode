"""Tests for the org member sync consumer (jeanclode.events.orgs.sync_members).

Covers:
- GitHub member sync creates ProviderIdentity + OrgMembership rows
- GitLab member sync creates ProviderIdentity + OrgMembership rows
- Sync is idempotent (re-running doesn't duplicate rows)
- GitHub orgs (no auth_token_encrypted) are NOT skipped — regression for the
  bug where the placeholder-org guard also blocked GitHub orgs
- GitLab placeholder orgs (no token) are correctly skipped
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import Response

from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.organizations import Organization, OrgMembership
from api.models.workspaces import Workspace
from api.routers.orgs.consumer import sync_org_members
from api.routers.orgs.schemas import SyncOrgMembersMessage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(app):
    """Fresh schema for each test."""
    Base.metadata.create_all(bind=app.database.engine)
    session = app.database.get_session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=app.database.engine)


@pytest.fixture
def github_org(db):
    """A claimed GitHub org (no auth_token_encrypted — uses installation tokens)."""
    ws = Workspace(name="test-ws", slug="test-ws")
    db.add(ws)
    db.flush()
    org = Organization(
        workspace_id=ws.id,
        name="test-org",
        external_org_id="123456",
        provider="github",
        installation_id="inst-999",
        base_url="https://github.com",
        auth_token_encrypted=None,  # GitHub orgs never store a token
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@pytest.fixture
def gitlab_org(db, app):
    """A GitLab org with an encrypted access token."""
    ws = Workspace(name="gl-ws", slug="gl-ws")
    db.add(ws)
    db.flush()
    encrypted = app.database.encrypt("gl-access-token")
    org = Organization(
        workspace_id=ws.id,
        name="gl-group",
        external_org_id="gl-42",
        provider="gitlab",
        base_url="https://gitlab.com",
        auth_token_encrypted=encrypted,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _fake_app(real_database, *, github=None, gitlab=None) -> MagicMock:
    """Build a fake Application with a real database and mock provider plugins.

    get_current_app() is patched in each test to return this object.
    The real database plugin is needed so ORM sessions and encrypt/decrypt work.
    """
    fake = MagicMock()
    fake.database = real_database
    fake.github = github
    fake.gitlab = gitlab
    return fake


def _paged_http_get(first_page: list[dict]) -> AsyncMock:
    """AsyncMock for http.get that returns first_page on page 1, [] on page 2+."""

    def _side_effect(url, **kwargs):
        page = kwargs.get("params", {}).get("page", 1)
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = first_page if page == 1 else []
        return resp

    return AsyncMock(side_effect=_side_effect)


# ---------------------------------------------------------------------------
# GitHub sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.orgs.consumer.get_current_app")
async def test_github_sync_creates_identities_and_memberships(
    mock_get_app, app, db, github_org
) -> None:
    """Consumer creates ProviderIdentity + OrgMembership for every GitHub org member."""
    github_mock = MagicMock()
    github_mock.get_installation_access_token = AsyncMock(return_value="inst-token")
    github_mock.http.get = _paged_http_get(
        [
            {"id": 1001, "login": "alice", "avatar_url": "https://github.com/alice.png"},
            {"id": 1002, "login": "bob", "avatar_url": "https://github.com/bob.png"},
        ]
    )
    mock_get_app.return_value = _fake_app(app.database, github=github_mock)

    await sync_org_members(SyncOrgMembersMessage(org_id=str(github_org.id), provider="github"))

    identities = db.query(ProviderIdentity).filter(ProviderIdentity.provider == "github").all()
    usernames = {i.username for i in identities}
    assert "alice" in usernames
    assert "bob" in usernames

    memberships = db.query(OrgMembership).filter(OrgMembership.org_id == github_org.id).all()
    assert len(memberships) == 2
    assert all(m.role == "member" for m in memberships)


@pytest.mark.asyncio
@patch("api.routers.orgs.consumer.get_current_app")
async def test_github_org_without_token_is_not_skipped(mock_get_app, app, db, github_org) -> None:
    """Regression: GitHub orgs have no auth_token_encrypted but must NOT be skipped."""
    assert github_org.auth_token_encrypted is None

    github_mock = MagicMock()
    github_mock.get_installation_access_token = AsyncMock(return_value="inst-token")
    github_mock.http.get = _paged_http_get([{"id": 2001, "login": "carol", "avatar_url": None}])
    mock_get_app.return_value = _fake_app(app.database, github=github_mock)

    await sync_org_members(SyncOrgMembersMessage(org_id=str(github_org.id), provider="github"))

    identity = db.query(ProviderIdentity).filter(ProviderIdentity.external_id == "2001").first()
    assert identity is not None, "GitHub member should be created despite org having no token"


@pytest.mark.asyncio
@patch("api.routers.orgs.consumer.get_current_app")
async def test_github_sync_is_idempotent(mock_get_app, app, db, github_org) -> None:
    """Running the sync twice must not create duplicate identities or memberships."""
    github_mock = MagicMock()
    github_mock.get_installation_access_token = AsyncMock(return_value="inst-token")
    github_mock.http.get = _paged_http_get([{"id": 3001, "login": "dan", "avatar_url": None}])
    mock_get_app.return_value = _fake_app(app.database, github=github_mock)

    msg = SyncOrgMembersMessage(org_id=str(github_org.id), provider="github")
    await sync_org_members(msg)
    await sync_org_members(msg)

    identities = db.query(ProviderIdentity).filter(ProviderIdentity.external_id == "3001").all()
    assert len(identities) == 1

    memberships = db.query(OrgMembership).filter(OrgMembership.org_id == github_org.id).all()
    assert len(memberships) == 1


# ---------------------------------------------------------------------------
# GitLab sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.orgs.consumer.get_current_app")
async def test_gitlab_sync_creates_identities_and_memberships(
    mock_get_app, app, db, gitlab_org
) -> None:
    """Consumer creates ProviderIdentity + OrgMembership for every GitLab group member."""
    gitlab_mock = MagicMock()
    gitlab_mock.http.get = _paged_http_get(
        [
            {"id": 501, "username": "eve", "avatar_url": None, "access_level": 40},
            {"id": 502, "username": "frank", "avatar_url": None, "access_level": 20},
        ]
    )
    mock_get_app.return_value = _fake_app(app.database, gitlab=gitlab_mock)

    await sync_org_members(SyncOrgMembersMessage(org_id=str(gitlab_org.id), provider="gitlab"))

    identities = db.query(ProviderIdentity).filter(ProviderIdentity.provider == "gitlab").all()
    usernames = {i.username for i in identities}
    assert "eve" in usernames
    assert "frank" in usernames

    memberships = db.query(OrgMembership).filter(OrgMembership.org_id == gitlab_org.id).all()
    assert len(memberships) == 2

    identity_map = {i.id: i for i in identities}
    role_map = {identity_map[m.provider_identity_id].username: m.role for m in memberships}
    assert role_map["eve"] == "admin"
    assert role_map["frank"] == "member"


# ---------------------------------------------------------------------------
# Placeholder org skipping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("api.routers.orgs.consumer.get_current_app")
async def test_gitlab_placeholder_org_is_skipped(mock_get_app, app, db) -> None:
    """GitLab ancestor placeholder orgs (no token) must be skipped."""
    ws = Workspace(name="ph-ws", slug="ph-ws")
    db.add(ws)
    db.flush()
    placeholder = Organization(
        workspace_id=ws.id,
        name="placeholder-group",
        external_org_id="ph-99",
        provider="gitlab",
        base_url="https://gitlab.com",
        auth_token_encrypted=None,
    )
    db.add(placeholder)
    db.commit()

    gitlab_mock = MagicMock()
    gitlab_mock.http.get = AsyncMock()
    mock_get_app.return_value = _fake_app(app.database, gitlab=gitlab_mock)

    await sync_org_members(SyncOrgMembersMessage(org_id=str(placeholder.id), provider="gitlab"))

    gitlab_mock.http.get.assert_not_called()
    assert db.query(ProviderIdentity).count() == 0
