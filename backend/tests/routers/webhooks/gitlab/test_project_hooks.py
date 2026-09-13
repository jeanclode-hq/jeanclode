"""Tests for GitLab project lifecycle hooks.

``project_create`` / ``project_destroy`` reach us two ways — a group webhook
with Project events enabled, or an instance-wide system hook — with an
identical payload, so both handlers run the same claim-and-materialize path.
A system hook fires for every project on the instance, so that path has to
keep only the projects a connected group actually owns.
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.database import (
    db_create_org,
    db_create_repository,
    db_create_workspace,
    db_get_org_by_external_id,
    db_get_repository_by_external_id,
)
from api.models import Base
from api.models.organizations import Provider
from api.routers.webhooks.gitlab.group_hook import handle_group_hook_event
from api.routers.webhooks.gitlab.system_hook import handle_system_hook_event

WEBHOOK_SECRET = "test-gitlab-webhook-secret"


@pytest.fixture
def gitlab_app(app):
    """App fixture with a GitLab plugin whose HTTP-calling methods get mocked."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.gitlab.config import GitLabPluginConfig
    from api.plugins.gitlab.plugin import GitLabPlugin

    gitlab_plugin = GitLabPlugin(
        GitLabPluginConfig(
            enabled=True,
            instance_url="https://gitlab.example.com",
            webhook_secret=WEBHOOK_SECRET,
        )
    )
    gitlab_plugin._http = AsyncMock()
    app._plugins.append(gitlab_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(gitlab_plugin)


@pytest.fixture
def connected_group(gitlab_app):
    """A connected top-level group holding a group access token."""
    db_plugin = gitlab_app.database
    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="500",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-500",
            base_url="https://gitlab.example.com",
            auth_token_encrypted=db_plugin.encrypt("group-token"),
        )
        return org.id


def _event(event_name: str, project_id: int, namespace_id: int, path: str) -> dict:
    """A system hook envelope as the webhook route queues it."""
    return {
        "instance_url": "https://gitlab.example.com",
        "payload": {
            "event_name": event_name,
            "project_id": project_id,
            "project_namespace_id": namespace_id,
            "path_with_namespace": path,
            "name": path.rsplit("/", 1)[-1],
        },
    }


def _project(project_id: int, path: str, namespace_id: int, namespace_name: str) -> dict:
    return {
        "id": project_id,
        "name": path.rsplit("/", 1)[-1],
        "path_with_namespace": path,
        "web_url": f"https://gitlab.example.com/{path}",
        "namespace": {"id": namespace_id, "name": namespace_name},
    }


@pytest.mark.asyncio
@patch("api.routers.webhooks.gitlab.projects.get_faststream_broker")
async def test_project_create_in_connected_group_creates_repo(
    mock_broker, gitlab_app, connected_group
):
    """A project created directly in the connected group lands in the DB."""
    mock_broker.return_value = AsyncMock()
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(
        return_value=_project(3001, "acme/new-service", 500, "acme")
    )

    result = await handle_system_hook_event(
        _event("project_create", 3001, 500, "acme/new-service"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3001")
        assert repo is not None
        assert repo.name == "acme/new-service"
        assert repo.web_url == "https://gitlab.example.com/acme/new-service"
        assert repo.org_id == connected_group

    # Its open MRs and issues predate any webhook we could have received.
    published = mock_broker.return_value.publish.call_args
    assert published.kwargs["stream"] == "jeanclode.events.gitlab.sync_project_repository"
    assert published.args[0].project_id == "3001"


@pytest.mark.asyncio
@patch("api.routers.webhooks.gitlab.projects.get_faststream_broker")
async def test_project_create_in_subgroup_creates_namespace_org(
    mock_broker, gitlab_app, connected_group
):
    """A project in a subgroup gets a placeholder org for its own namespace."""
    mock_broker.return_value = AsyncMock()
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(
        return_value=_project(3002, "acme/platform/svc", 600, "platform")
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[{"id": "500", "name": "acme"}])

    result = await handle_system_hook_event(
        _event("project_create", 3002, 600, "acme/platform/svc"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3002")
        platform_org = db_get_org_by_external_id(db, "600", provider="gitlab")
        assert repo is not None
        assert platform_org is not None
        assert repo.org_id == platform_org.id
        assert platform_org.parent_org_id == connected_group


@pytest.mark.asyncio
async def test_project_outside_connected_groups_is_ignored(gitlab_app, connected_group):
    """A public project the token can read but does not own stays untracked.

    A group token reads public projects instance-wide, so a successful fetch
    is not proof of ownership — only the namespace chain is.
    """
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(
        return_value=_project(4001, "other-org/public-thing", 900, "other-org")
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[])

    result = await handle_system_hook_event(
        _event("project_create", 4001, 900, "other-org/public-thing"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "4001") is None


@pytest.mark.asyncio
async def test_project_the_token_cannot_read_is_ignored(gitlab_app, connected_group):
    """A private project on another group 404s for every token we hold."""
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(side_effect=Exception("404 Project Not Found"))

    result = await handle_system_hook_event(
        _event("project_create", 4002, 901, "private-org/secret"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "4002") is None


@pytest.mark.asyncio
async def test_project_destroy_removes_repo(gitlab_app, connected_group):
    """A deleted project stops being dispatchable."""
    with gitlab_app.database.session() as db:
        db_create_repository(
            db=db,
            org_id=connected_group,
            external_id="3003",
            name="acme/gone",
            web_url="https://gitlab.example.com/acme/gone",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )

    result = await handle_system_hook_event(
        _event("project_destroy", 3003, 500, "acme/gone"),
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "3003") is None


@pytest.mark.asyncio
@patch("api.routers.webhooks.gitlab.projects.get_faststream_broker")
async def test_project_transfer_moves_the_existing_row(mock_broker, gitlab_app, connected_group):
    """A transferred project follows its new namespace instead of duplicating."""
    mock_broker.return_value = AsyncMock()
    with gitlab_app.database.session() as db:
        db_create_repository(
            db=db,
            org_id=connected_group,
            external_id="3004",
            name="acme/moving",
            web_url="https://gitlab.example.com/acme/moving",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(
        return_value=_project(3004, "acme/platform/moving", 600, "platform")
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[{"id": "500", "name": "acme"}])

    result = await handle_system_hook_event(
        _event("project_transfer", 3004, 600, "acme/platform/moving"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3004")
        platform_org = db_get_org_by_external_id(db, "600", provider="gitlab")
        assert repo is not None
        assert repo.name == "acme/platform/moving"
        assert repo.org_id == platform_org.id

    # Already tracked — its MRs and issues arrive by webhook, no backfill.
    mock_broker.return_value.publish.assert_not_called()


@pytest.mark.asyncio
async def test_event_without_connected_gitlab_org_is_a_noop(gitlab_app):
    """No connected group at all means nothing to match against."""
    result = await handle_system_hook_event(
        _event("project_create", 5001, 42, "somewhere/thing"),
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "5001") is None


@pytest.mark.asyncio
@patch("api.routers.webhooks.gitlab.projects.get_faststream_broker")
async def test_group_hook_project_create_creates_repo(mock_broker, gitlab_app, connected_group):
    """A group webhook with Project events enabled needs no admin access."""
    mock_broker.return_value = AsyncMock()
    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_project = AsyncMock(
        return_value=_project(3010, "acme/from-group-hook", 500, "acme")
    )

    result = await handle_group_hook_event(
        _event("project_create", 3010, 500, "acme/from-group-hook"),
        gitlab_app.database,
        gitlab_plugin,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "3010")
        assert repo is not None
        assert repo.org_id == connected_group


@pytest.mark.asyncio
async def test_group_hook_project_destroy_removes_repo(gitlab_app, connected_group):
    """Deletion arrives on the group hook too, and means the same thing."""
    with gitlab_app.database.session() as db:
        db_create_repository(
            db=db,
            org_id=connected_group,
            external_id="3011",
            name="acme/short-lived",
            web_url="https://gitlab.example.com/acme/short-lived",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )

    result = await handle_group_hook_event(
        _event("project_destroy", 3011, 500, "acme/short-lived"),
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "3011") is None


@pytest.mark.asyncio
async def test_subgroup_destroy_removes_the_org_and_its_repos(gitlab_app, connected_group):
    """A deleted subgroup takes its placeholder org — and its projects — with it."""
    with gitlab_app.database.session() as db:
        parent = db_get_org_by_external_id(db, "500", provider="gitlab")
        subgroup = db_create_org(
            db=db,
            workspace_id=parent.workspace_id,
            name="platform",
            external_org_id="600",
            provider=Provider.GITLAB.value,
            base_url="https://gitlab.example.com",
            parent_org_id=connected_group,
            root_org_id=connected_group,
        )
        db_create_repository(
            db=db,
            org_id=subgroup.id,
            external_id="3012",
            name="acme/platform/svc",
            web_url="https://gitlab.example.com/acme/platform/svc",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )

    result = await handle_group_hook_event(
        {
            "instance_url": "https://gitlab.example.com",
            "payload": {
                "event_name": "subgroup_destroy",
                "group_id": 600,
                "full_path": "acme/platform",
                "parent_group_id": 500,
            },
        },
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_org_by_external_id(db, "600", provider="gitlab") is None
        assert db_get_repository_by_external_id(db, "3012") is None
        # The group above it is untouched.
        assert db_get_org_by_external_id(db, "500", provider="gitlab") is not None


@pytest.mark.asyncio
async def test_subgroup_create_is_deferred(gitlab_app, connected_group):
    """An empty subgroup earns no org row until a project shows up in it."""
    result = await handle_group_hook_event(
        {
            "instance_url": "https://gitlab.example.com",
            "payload": {
                "event_name": "subgroup_create",
                "group_id": 700,
                "full_path": "acme/empty",
                "parent_group_id": 500,
            },
        },
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_org_by_external_id(db, "700", provider="gitlab") is None


@pytest.mark.asyncio
async def test_group_destroy_removes_the_connected_org(gitlab_app, connected_group):
    """Only a system hook reports a top-level group's deletion."""
    result = await handle_system_hook_event(
        {
            "instance_url": "https://gitlab.example.com",
            "payload": {"event_name": "group_destroy", "group_id": 500, "full_path": "acme"},
        },
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_org_by_external_id(db, "500", provider="gitlab") is None


@pytest.mark.asyncio
async def test_group_destroy_for_an_unconnected_group_is_a_noop(gitlab_app, connected_group):
    """The hook fires for every group on the instance, most of them not ours."""
    result = await handle_system_hook_event(
        {
            "instance_url": "https://gitlab.example.com",
            "payload": {
                "event_name": "group_destroy",
                "group_id": 999,
                "full_path": "someone-else",
            },
        },
        gitlab_app.database,
        gitlab_app.gitlab,
    )

    assert result.processed is True
    with gitlab_app.database.session() as db:
        assert db_get_org_by_external_id(db, "500", provider="gitlab") is not None
