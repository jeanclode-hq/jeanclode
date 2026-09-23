"""Tests for the GitLab group repository sync consumer.

Covers the subgroup org-linking + full-path naming fix: a group/subgroup
token sync (``sync_gitlab_group_repositories``) must link each project to
its own immediate namespace org — not the token's top-level org — and store
its full ``path_with_namespace`` as the name, so two same-named projects in
different subgroups (e.g. ``team/atlas/atlas`` and ``team/qa/atlas``)
don't collide in the dashboard. See issue #171 (S1).
"""

from unittest.mock import AsyncMock

import pytest

from api.database import (
    db_create_org,
    db_create_workspace,
    db_get_org_by_external_id,
    db_get_repository_by_external_id,
)
from api.models import Base
from api.models.organizations import Provider
from api.routers.sources.gitlab.consumer import sync_gitlab_group_repositories
from api.routers.sources.gitlab.schemas import GitLabSyncGroupRepositoriesMessage


@pytest.fixture
def gitlab_app(app):
    """App fixture with a GitLab plugin whose HTTP-calling methods get mocked."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.gitlab.config import GitLabPluginConfig
    from api.plugins.gitlab.plugin import GitLabPlugin

    gitlab_config = GitLabPluginConfig(enabled=True, instance_url="https://gitlab.com")
    gitlab_plugin = GitLabPlugin(gitlab_config)
    gitlab_plugin._http = AsyncMock()
    app._plugins.append(gitlab_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(gitlab_plugin)


def _project(
    project_id: int, name: str, path_with_namespace: str, namespace_id: int, namespace_name: str
) -> dict:
    return {
        "id": project_id,
        "name": name,
        "path_with_namespace": path_with_namespace,
        "web_url": f"https://gitlab.com/{path_with_namespace}",
        "namespace": {"id": namespace_id, "name": namespace_name},
    }


@pytest.mark.asyncio
async def test_sync_links_subgroup_projects_to_own_namespace_org(gitlab_app):
    """Two same-named projects in different subgroups get distinct orgs and full-path names."""
    db_plugin = gitlab_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="team-platform", slug="team-platform")
        top_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="team-platform",
            external_org_id="500",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-500",
            auth_token_encrypted=db_plugin.encrypt("real-group-token"),
        )
        top_org_id = top_org.id

    gitlab_plugin = gitlab_app.gitlab

    projects = [
        _project(1001, "atlas", "team-platform/atlas/atlas", 600, "atlas"),
        _project(1002, "atlas", "team-platform/qa/atlas", 700, "qa"),
        _project(1003, "core-lib", "team-platform/core-lib", 500, "team-platform"),
    ]
    gitlab_plugin.fetch_group_projects = AsyncMock(return_value=projects)

    async def _fake_ancestors(access_token, group_id, provider_url=None, known_parent_id=None):
        # Both "atlas" (600) and "qa" (700) subgroups are direct children
        # of the top-level group (500) — matches the reported gitlab.example.org
        # layout (team-platform/atlas/atlas vs .../qa/atlas).
        assert group_id in ("600", "700")
        return [{"id": "500", "name": "team-platform"}]

    gitlab_plugin.fetch_group_ancestors = AsyncMock(side_effect=_fake_ancestors)
    gitlab_plugin.fetch_project_merge_requests = AsyncMock(return_value=[])
    gitlab_plugin.fetch_project_issues = AsyncMock(return_value=[])

    message = GitLabSyncGroupRepositoriesMessage(org_id=str(top_org_id), group_id="500")
    await sync_gitlab_group_repositories(message)

    with db_plugin.session() as db:
        repo_atlas_sub = db_get_repository_by_external_id(db, "1001")
        repo_qa = db_get_repository_by_external_id(db, "1002")
        repo_core = db_get_repository_by_external_id(db, "1003")

        assert repo_atlas_sub is not None
        assert repo_qa is not None
        assert repo_core is not None

        # Full path stored as the name, not the bare project name — this is
        # what the dashboard splits into displayName / subgroupPath.
        assert repo_atlas_sub.name == "team-platform/atlas/atlas"
        assert repo_qa.name == "team-platform/qa/atlas"
        assert repo_core.name == "team-platform/core-lib"

        # Each subgroup project lands under its OWN namespace org, not the
        # top-level org — and the two same-named "atlas" projects must not
        # collide under a shared org (the reported bug).
        assert repo_atlas_sub.org_id != top_org_id
        assert repo_qa.org_id != top_org_id
        assert repo_atlas_sub.org_id != repo_qa.org_id

        # The project directly in the top-level group takes the fast path.
        assert repo_core.org_id == top_org_id

        atlas_org = db_get_org_by_external_id(db, "600", provider="gitlab")
        qa_org = db_get_org_by_external_id(db, "700", provider="gitlab")
        assert atlas_org is not None
        assert qa_org is not None
        assert repo_atlas_sub.org_id == atlas_org.id
        assert repo_qa.org_id == qa_org.id

        # Auto-created as placeholders — hierarchy metadata only, no token of
        # their own (repos authenticate via the repo-level token the sync
        # stamps onto each repo it creates, since their org may be a
        # token-less placeholder).
        assert atlas_org.auth_token_encrypted is None
        assert atlas_org.parent_org_id == top_org_id
        assert atlas_org.root_org_id == top_org_id
        assert qa_org.auth_token_encrypted is None
        assert qa_org.parent_org_id == top_org_id
        assert qa_org.root_org_id == top_org_id

        assert repo_atlas_sub.auth_token_encrypted is not None
        assert repo_qa.auth_token_encrypted is not None


@pytest.mark.asyncio
async def test_sync_ensures_trigger_labels_on_the_group(gitlab_app):
    """The group sync calls ensure_group_labels once, on the top-level group."""
    db_plugin = gitlab_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        top_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="900",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-900",
            auth_token_encrypted=db_plugin.encrypt("real-group-token"),
        )
        top_org_id = top_org.id

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.ensure_group_labels = AsyncMock()
    gitlab_plugin.fetch_group_projects = AsyncMock(return_value=[])

    message = GitLabSyncGroupRepositoriesMessage(org_id=str(top_org_id), group_id="900")
    await sync_gitlab_group_repositories(message)

    gitlab_plugin.ensure_group_labels.assert_awaited_once_with(
        "real-group-token", "900", provider_url="https://gitlab.com"
    )


@pytest.mark.asyncio
async def test_sync_reuses_existing_namespace_org_across_projects(gitlab_app):
    """Two projects in the same subgroup share one namespace org, not one each."""
    db_plugin = gitlab_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        top_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="10",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-10",
            auth_token_encrypted=db_plugin.encrypt("real-group-token"),
        )
        top_org_id = top_org.id

    gitlab_plugin = gitlab_app.gitlab
    projects = [
        _project(2001, "svc-a", "acme/platform/svc-a", 20, "platform"),
        _project(2002, "svc-b", "acme/platform/svc-b", 20, "platform"),
    ]
    gitlab_plugin.fetch_group_projects = AsyncMock(return_value=projects)
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[{"id": "10", "name": "acme"}])
    gitlab_plugin.fetch_project_merge_requests = AsyncMock(return_value=[])
    gitlab_plugin.fetch_project_issues = AsyncMock(return_value=[])

    message = GitLabSyncGroupRepositoriesMessage(org_id=str(top_org_id), group_id="10")
    await sync_gitlab_group_repositories(message)

    with db_plugin.session() as db:
        repo_a = db_get_repository_by_external_id(db, "2001")
        repo_b = db_get_repository_by_external_id(db, "2002")
        assert repo_a is not None
        assert repo_b is not None
        assert repo_a.org_id == repo_b.org_id
        assert repo_a.org_id != top_org_id

        platform_org = db_get_org_by_external_id(db, "20", provider="gitlab")
        assert platform_org is not None
        assert repo_a.org_id == platform_org.id
        assert platform_org.parent_org_id == top_org_id

    # The namespace-org cache means the second project's ancestor lookup is
    # skipped once the "platform" org exists from the first.
    assert gitlab_plugin.fetch_group_ancestors.await_count == 1


@pytest.mark.asyncio
async def test_sync_creates_project_hooks_when_manage_project_webhooks_is_set(gitlab_app):
    """With the setting on, the sync ensures the Jeanclode webhook on every project."""
    db_plugin = gitlab_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="free", slug="free")
        top_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="free",
            external_org_id="90",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-90",
            auth_token_encrypted=db_plugin.encrypt("group-token"),
            settings={"manage_project_webhooks": True},
        )
        top_org_id = top_org.id

    gitlab_plugin = gitlab_app.gitlab
    projects = [
        _project(3001, "svc-a", "free/svc-a", 90, "free"),
        _project(3002, "svc-b", "free/svc-b", 90, "free"),
    ]
    gitlab_plugin.fetch_group_projects = AsyncMock(return_value=projects)
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[{"id": "90", "name": "free"}])
    gitlab_plugin.fetch_project_merge_requests = AsyncMock(return_value=[])
    gitlab_plugin.fetch_project_issues = AsyncMock(return_value=[])
    gitlab_plugin.get_effective_webhook_url = lambda: "https://jc.example.com/webhooks/gitlab"
    gitlab_plugin.get_effective_webhook_secret = lambda: "shhh"
    gitlab_plugin.ensure_project_webhook = AsyncMock(return_value="created")

    message = GitLabSyncGroupRepositoriesMessage(org_id=str(top_org_id), group_id="90")
    await sync_gitlab_group_repositories(message)

    hooked = {call.args[1] for call in gitlab_plugin.ensure_project_webhook.await_args_list}
    assert hooked == {"3001", "3002"}
    for call in gitlab_plugin.ensure_project_webhook.await_args_list:
        assert call.kwargs["hook_url"] == "https://jc.example.com/webhooks/gitlab"
        assert call.kwargs["secret"] == "shhh"


@pytest.mark.asyncio
async def test_sync_skips_project_hooks_by_default(gitlab_app):
    """Without the setting, the sync never touches project webhooks."""
    db_plugin = gitlab_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="premium", slug="premium")
        top_org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="premium",
            external_org_id="91",
            provider=Provider.GITLAB.value,
            installation_id="gitlab-group-91",
            auth_token_encrypted=db_plugin.encrypt("group-token"),
        )
        top_org_id = top_org.id

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.fetch_group_projects = AsyncMock(
        return_value=[_project(3101, "svc", "premium/svc", 91, "premium")]
    )
    gitlab_plugin.fetch_group_ancestors = AsyncMock(return_value=[{"id": "91", "name": "premium"}])
    gitlab_plugin.fetch_project_merge_requests = AsyncMock(return_value=[])
    gitlab_plugin.fetch_project_issues = AsyncMock(return_value=[])
    gitlab_plugin.ensure_project_webhook = AsyncMock()

    message = GitLabSyncGroupRepositoriesMessage(org_id=str(top_org_id), group_id="91")
    await sync_gitlab_group_repositories(message)

    gitlab_plugin.ensure_project_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_consumer_deletes_each_project_hook_with_its_own_token(gitlab_app):
    """The teardown consumer decrypts each project's token and removes its hook."""
    from api.routers.sources.gitlab.consumer import teardown_gitlab_project_hooks
    from api.routers.sources.gitlab.schemas import (
        GitLabProjectHookTeardown,
        GitLabProjectHookTeardownMessage,
    )

    gitlab_plugin = gitlab_app.gitlab
    gitlab_plugin.get_effective_webhook_url = lambda: "https://jc.example.com/webhooks/gitlab"
    gitlab_plugin.delete_project_webhook = AsyncMock(return_value="deleted")

    db = gitlab_app.database
    message = GitLabProjectHookTeardownMessage(
        projects=[
            GitLabProjectHookTeardown(
                project_id="11",
                provider_url="https://gitlab.example.dev",
                encrypted_token=db.encrypt("tok-11"),
            ),
            GitLabProjectHookTeardown(
                project_id="22",
                provider_url="https://gitlab.example.dev",
                encrypted_token=db.encrypt("tok-22"),
            ),
        ]
    )
    await teardown_gitlab_project_hooks(message)

    used = {
        c.args[1]: (c.args[0], c.kwargs["provider_url"])
        for c in gitlab_plugin.delete_project_webhook.await_args_list
    }
    assert used == {
        "11": ("tok-11", "https://gitlab.example.dev"),
        "22": ("tok-22", "https://gitlab.example.dev"),
    }
