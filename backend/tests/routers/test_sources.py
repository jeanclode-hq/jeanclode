"""Tests for GitLab sources endpoint."""

from unittest.mock import AsyncMock, patch

import pytest

from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.users import User
from api.plugins.web.session import SessionData


@pytest.fixture
def gitlab_app(app):
    """App fixture with GitLab plugin enabled and tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.gitlab.config import GitLabPluginConfig
    from api.plugins.gitlab.plugin import GitLabPlugin

    gitlab_config = GitLabPluginConfig(enabled=True, instance_url="https://gitlab.com")
    gitlab_plugin = GitLabPlugin(gitlab_config)
    # Mock startup (no real HTTP client needed)
    gitlab_plugin._http = AsyncMock()
    app._plugins.append(gitlab_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(gitlab_plugin)


@pytest.fixture
def _mock_session(gitlab_app):
    """Mock session for authentication."""
    with gitlab_app.database.session() as db:
        user = User(email="auth@example.com")
        db.add(user)
        db.flush()
        identity = ProviderIdentity(
            provider="gitlab",
            external_id="gitlab-user-1",
            username="authuser",
            user_id=user.id,
        )
        db.add(identity)
        db.commit()
        user_id = str(user.id)

    data = SessionData(
        user_id=user_id,
        created_at="2024-01-01T00:00:00+00:00",
        last_active="2024-01-01T00:00:00+00:00",
    )
    sessions = gitlab_app.web.sessions

    with (
        patch.object(sessions, "get", new_callable=AsyncMock, return_value=data),
        patch.object(sessions, "refresh", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def gitlab_client(gitlab_app, _mock_session):
    """Test client with GitLab plugin and authentication."""
    from fastapi.testclient import TestClient

    return TestClient(
        gitlab_app.web.get_asgi_app(),
        cookies={"jeanclode_session": "test-session"},
    )


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_setup_creates_git_org(mock_broker, gitlab_client, gitlab_app):
    """POST /sources/gitlab with valid group token creates git org."""
    mock_broker.return_value = AsyncMock()

    # Mock the GitLab plugin methods
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.verify_token = AsyncMock(
        return_value={
            "id": 999,
            "username": "group_42_bot_abc",
            "bot": True,
            "name": "Bot",
        }
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={
            "id": 42,
            "name": "my-group",
            "avatar_url": "https://gitlab.com/avatar.png",
        }
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    response = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "glpat-test-token"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["source_type"] == "group"
    assert data["source_name"] == "my-group"
    assert data["repos_synced"] is True


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_manage_project_webhooks_persisted_to_settings(
    mock_broker, gitlab_client, gitlab_app
):
    """manage_project_webhooks=True on the add-source request lands in org settings."""
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "group_77_bot_x", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_group = AsyncMock(return_value={"id": 77, "name": "free-group"})
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    response = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "glpat-test-token", "manage_project_webhooks": True},
    )

    assert response.status_code == 200
    from uuid import UUID

    from api.database import db_get_org_by_id

    org_id = UUID(response.json()["source_id"])
    with gitlab_app.database.session() as db:
        org = db_get_org_by_id(db, org_id)
        assert org.settings.get("manage_project_webhooks") is True


def test_gitlab_invalid_token(gitlab_client, gitlab_app):
    """POST /sources/gitlab with invalid token returns error."""
    from fastapi import HTTPException

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.verify_token = AsyncMock(
        side_effect=HTTPException(status_code=401, detail="Invalid GitLab access token")
    )

    response = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "invalid-token"},
    )

    assert response.status_code == 401


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_group_token_replay_upgrades_token(mock_broker, gitlab_client, gitlab_app):
    """Replaying POST /sources/gitlab with a new group token upgrades the stored token."""
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.verify_token = AsyncMock(
        return_value={
            "id": 999,
            "username": "group_42_bot_abc",
            "bot": True,
            "name": "Bot",
        }
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={
            "id": 42,
            "name": "my-group",
            "avatar_url": None,
            "parent_id": None,
        }
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    # First call — creates the org
    response1 = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "glpat-token-1"},
    )
    assert response1.status_code == 200

    # Second call — upgrades token, returns 200
    response2 = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "glpat-token-2"},
    )
    assert response2.status_code == 200
    assert response2.json()["source_name"] == "my-group"


def test_gitlab_personal_token_rejected(gitlab_client, gitlab_app):
    """POST /sources/gitlab with personal token returns 400."""
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.verify_token = AsyncMock(
        return_value={
            "id": 999,
            "username": "regular-user",
            "bot": False,
            "name": "Regular User",
        }
    )

    response = gitlab_client.post(
        "/sources/gitlab",
        json={"access_token": "glpat-personal-token"},
    )

    assert response.status_code == 400


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_group_token_upgrades_project_org(mock_broker, gitlab_client, gitlab_app):
    """US-3: Connecting a subgroup token upgrades an existing project-token org.

    Org gets group token; project-level repo tokens are cleared so they inherit.
    """
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab

    # Step 1: connect project token for project 99 in namespace (group) 42
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "project_99_bot_abc", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_project = AsyncMock(
        return_value={
            "id": 99,
            "name": "my-project",
            "path_with_namespace": "my-group/my-project",
            "web_url": "https://gitlab.com/my-group/my-project",
            "namespace": {"id": 42, "name": "my-group", "kind": "group", "avatar_url": None},
            "avatar_url": None,
        }
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    r1 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-project-token"})
    assert r1.status_code == 200
    assert r1.json()["source_type"] == "project"

    # Verify repo has a token stored
    with gitlab_app.database.session() as db:
        from api.database import db_get_repository_by_external_id

        repo = db_get_repository_by_external_id(db, "99")
        assert repo is not None
        assert repo.auth_token_encrypted is not None

    # Step 2: connect subgroup token for group 42 (same namespace)
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 2, "username": "group_42_bot_xyz", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={"id": 42, "name": "my-group", "avatar_url": None, "parent_id": None}
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    r2 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-group-token"})
    assert r2.status_code == 200
    data = r2.json()
    assert data["source_type"] == "group"
    assert data["source_name"] == "my-group"

    # Org should now have the group token; repo token should be cleared
    with gitlab_app.database.session() as db:
        from api.database import db_get_org_by_external_id, db_get_repository_by_external_id

        org = db_get_org_by_external_id(db, "42", provider="gitlab")
        assert org is not None
        assert org.auth_token_encrypted is not None

        repo = db_get_repository_by_external_id(db, "99")
        assert repo is not None
        assert repo.auth_token_encrypted is None  # cleared — inherits from org


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_second_project_reuses_namespace_org(mock_broker, gitlab_client, gitlab_app):
    """US-7: Adding a second project from the same namespace reuses the existing org."""
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    # Step 1: connect project 99 in namespace 42
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "project_99_bot_abc", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_project = AsyncMock(
        return_value={
            "id": 99,
            "name": "project-a",
            "path_with_namespace": "my-group/project-a",
            "web_url": "https://gitlab.com/my-group/project-a",
            "namespace": {"id": 42, "name": "my-group", "kind": "group", "avatar_url": None},
            "avatar_url": None,
        }
    )

    r1 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-proj-a"})
    assert r1.status_code == 200

    # Step 2: connect project 100 in same namespace 42
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 2, "username": "project_100_bot_xyz", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_project = AsyncMock(
        return_value={
            "id": 100,
            "name": "project-b",
            "path_with_namespace": "my-group/project-b",
            "web_url": "https://gitlab.com/my-group/project-b",
            "namespace": {"id": 42, "name": "my-group", "kind": "group", "avatar_url": None},
            "avatar_url": None,
        }
    )

    r2 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-proj-b"})
    assert r2.status_code == 200
    data = r2.json()
    assert data["source_type"] == "project"
    assert data["source_name"] == "my-group"

    # There should be exactly one org for namespace 42
    with gitlab_app.database.session() as db:
        from api.database import db_get_repository_by_external_id
        from api.models.organizations import Organization

        orgs = db.query(Organization).filter(Organization.external_org_id == "42").all()
        assert len(orgs) == 1

        repo_a = db_get_repository_by_external_id(db, "99")
        repo_b = db_get_repository_by_external_id(db, "100")
        assert repo_a is not None
        assert repo_b is not None
        assert repo_a.org_id == repo_b.org_id

        # Both repos store their own project tokens (org has no group token)
        assert repo_a.auth_token_encrypted is not None
        assert repo_b.auth_token_encrypted is not None


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_project_token_with_colliding_external_id_from_other_org(
    mock_broker, gitlab_client, gitlab_app
):
    """Regression: Repository.external_id is only unique per-org (uq_repository_org_external),
    so a repo in an unrelated org/provider (e.g. a Sentry project) can share the same
    external_id as a new GitLab project. That must not be mistaken for "already connected"
    and silently skip creating the repo / storing its token.
    """
    mock_broker.return_value = AsyncMock()

    # Seed an unrelated org + repo (different provider, different workspace) that happens
    # to share external_id "197" with the GitLab project we're about to connect.
    with gitlab_app.database.session() as db:
        from api.database import db_create_org, db_create_repository, db_create_workspace

        other_workspace = db_create_workspace(db, name="other", slug="other")
        other_org = db_create_org(
            db=db,
            workspace_id=other_workspace.id,
            name="other-org",
            external_org_id="sentry-org-1",
            provider="sentry",
        )
        db_create_repository(
            db=db,
            org_id=other_org.id,
            external_id="197",
            name="unrelated-sentry-project",
            provider="sentry",
        )

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "project_197_bot_abc", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_project = AsyncMock(
        return_value={
            "id": 197,
            "name": "orders-api",
            "path_with_namespace": "jdoe/orders-api",
            "web_url": "https://gitlab.com/jdoe/orders-api",
            "namespace": {"id": 7, "name": "adsa", "kind": "user", "avatar_url": None},
            "avatar_url": None,
        }
    )

    response = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-project-token"})
    assert response.status_code == 200

    with gitlab_app.database.session() as db:
        from api.database import (
            db_get_org_by_external_id,
            db_get_repository_by_org_and_external_id,
        )

        org = db_get_org_by_external_id(db, "7", provider="gitlab")
        assert org is not None

        repo = db_get_repository_by_org_and_external_id(db, org.id, "197")
        assert repo is not None
        assert repo.provider == "gitlab"
        assert repo.auth_token_encrypted is not None


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_second_project_inherits_group_token(mock_broker, gitlab_client, gitlab_app):
    """US-7: Second project added when org already has group token inherits — no repo token stored."""
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    # Step 1: connect group token for group 42
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "group_42_bot_abc", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={"id": 42, "name": "my-group", "avatar_url": None, "parent_id": None}
    )

    r1 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-group-token"})
    assert r1.status_code == 200

    # Step 2: add a project from that namespace via project token
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 2, "username": "project_55_bot_xyz", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_project = AsyncMock(
        return_value={
            "id": 55,
            "name": "extra-project",
            "path_with_namespace": "my-group/extra-project",
            "web_url": "https://gitlab.com/my-group/extra-project",
            "namespace": {"id": 42, "name": "my-group", "kind": "group", "avatar_url": None},
            "avatar_url": None,
        }
    )

    r2 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-project-token"})
    assert r2.status_code == 200

    # Repo should NOT have a token stored — it inherits from the org
    with gitlab_app.database.session() as db:
        from api.database import db_get_repository_by_external_id

        repo = db_get_repository_by_external_id(db, "55")
        assert repo is not None
        assert repo.auth_token_encrypted is None


@patch("api.routers.sources.gitlab.route.get_faststream_broker")
def test_gitlab_parent_group_token_clears_subgroup_token(mock_broker, gitlab_client, gitlab_app):
    """Connecting a subgroup token, then later the parent group's token, demotes
    the subgroup's own org token back to a placeholder — the parent's token
    already covers it via GitLab's inherited permissions, so keeping both
    around is a stale, redundant credential (mirrors how a project's own
    token is cleared once its org gets a group token — see
    test_gitlab_group_token_upgrades_project_org — one level up the hierarchy).
    """
    mock_broker.return_value = AsyncMock()

    gitlab_plugin = gitlab_app.gitlab

    # Step 1: connect a subgroup token for group 99, whose immediate parent
    # is top-level group 42. This auto-creates group 42 as a token-less
    # placeholder ancestor org (S4).
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 1, "username": "group_99_bot_abc", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={"id": 99, "name": "my-subgroup", "avatar_url": None, "parent_id": 42}
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(
        return_value=[{"id": 42, "name": "my-group", "avatar_url": None}]
    )

    r1 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-subgroup-token"})
    assert r1.status_code == 200

    with gitlab_app.database.session() as db:
        from api.database import db_get_org_by_external_id

        subgroup_org = db_get_org_by_external_id(db, "99", provider="gitlab")
        assert subgroup_org is not None
        assert subgroup_org.auth_token_encrypted is not None

        parent_org = db_get_org_by_external_id(db, "42", provider="gitlab")
        assert parent_org is not None
        assert parent_org.auth_token_encrypted is None  # placeholder ancestor
        assert parent_org.installation_id is None

    # Step 2: now connect the parent group (42) itself with its own token.
    gitlab_plugin.verify_token = AsyncMock(
        return_value={"id": 2, "username": "group_42_bot_xyz", "bot": True, "name": "Bot"}
    )
    gitlab_plugin.fetch_group = AsyncMock(
        return_value={"id": 42, "name": "my-group", "avatar_url": None, "parent_id": None}
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    r2 = gitlab_client.post("/sources/gitlab", json={"access_token": "glpat-parent-token"})
    assert r2.status_code == 200
    assert r2.json()["source_name"] == "my-group"

    with gitlab_app.database.session() as db:
        from api.database import db_get_org_by_external_id

        parent_org = db_get_org_by_external_id(db, "42", provider="gitlab")
        assert parent_org is not None
        assert parent_org.auth_token_encrypted is not None  # now has its own token

        # The subgroup's own token is superseded and cleared — it's now a
        # placeholder that inherits from the parent, exactly like a
        # project-token repo is demoted when its org gets a group token.
        subgroup_org = db_get_org_by_external_id(db, "99", provider="gitlab")
        assert subgroup_org is not None
        assert subgroup_org.auth_token_encrypted is None
        assert subgroup_org.installation_id is None
        assert subgroup_org.parent_org_id == parent_org.id
