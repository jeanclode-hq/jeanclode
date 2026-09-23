"""Tests for the trigger-label auto-creation hooked into the GitHub repo sync
consumers (``sync_installation_repositories`` for a fresh install,
``sync_added_repositories`` for repos added later).
"""

from unittest.mock import AsyncMock

import pytest

from api.database import db_create_org, db_create_workspace
from api.models import Base
from api.models.organizations import Provider
from api.routers.webhooks.github.consumer import (
    sync_added_repositories,
    sync_installation_repositories,
)
from api.routers.webhooks.github.schemas import (
    GitHubSyncInstallationMessage,
    GitHubSyncRepositoriesMessage,
)


@pytest.fixture
def github_app(app):
    """App fixture with a GitHub plugin whose HTTP-calling methods get mocked."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.github.config import GitHubPluginConfig
    from api.plugins.github.plugin import GitHubPlugin

    github_plugin = GitHubPlugin(GitHubPluginConfig())
    github_plugin._http = AsyncMock()
    app._plugins.append(github_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(github_plugin)


def _repo(repo_id: int, name: str, full_name: str) -> dict:
    return {
        "id": repo_id,
        "name": name,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "owner": {"avatar_url": "https://github.com/avatar.png"},
    }


@pytest.mark.asyncio
async def test_installation_sync_ensures_labels_on_every_repo(github_app):
    db_plugin = github_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        org = db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="1",
            provider=Provider.GITHUB.value,
            installation_id="123",
            base_url="https://github.com",
        )
        org_id = org.id

    github_plugin = github_app.github
    github_plugin.get_installation_access_token = AsyncMock(return_value="install-token")
    github_plugin.fetch_installation_repositories = AsyncMock(
        return_value=[
            _repo(1001, "app", "acme/app"),
            _repo(1002, "infra", "acme/infra"),
        ]
    )
    github_plugin.ensure_repo_labels = AsyncMock()

    await sync_installation_repositories(GitHubSyncInstallationMessage(installation_id="123"))

    assert org_id  # sanity: org actually persisted before the sync ran
    called_with = {call.args[1] for call in github_plugin.ensure_repo_labels.await_args_list}
    assert called_with == {"acme/app", "acme/infra"}
    for call in github_plugin.ensure_repo_labels.await_args_list:
        assert call.args[0] == "install-token"


@pytest.mark.asyncio
async def test_added_repos_sync_ensures_labels_on_new_repo(github_app):
    db_plugin = github_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="1",
            provider=Provider.GITHUB.value,
            installation_id="123",
            base_url="https://github.com",
        )

    github_plugin = github_app.github
    github_plugin.get_installation_access_token = AsyncMock(return_value="install-token")
    github_plugin.fetch_repository_by_id = AsyncMock(
        return_value=_repo(2001, "new-repo", "acme/new-repo")
    )
    github_plugin.ensure_repo_labels = AsyncMock()

    message = GitHubSyncRepositoriesMessage(installation_id="123", external_repo_ids=["2001"])
    await sync_added_repositories(message)

    github_plugin.ensure_repo_labels.assert_awaited_once_with("install-token", "acme/new-repo")


@pytest.mark.asyncio
async def test_label_failure_does_not_abort_the_sync(github_app):
    """A repo whose label call fails still gets its PR/issue backfill attempted."""
    db_plugin = github_app.database

    with db_plugin.session() as db:
        workspace = db_create_workspace(db, name="acme", slug="acme")
        db_create_org(
            db=db,
            workspace_id=workspace.id,
            name="acme",
            external_org_id="1",
            provider=Provider.GITHUB.value,
            installation_id="123",
            base_url="https://github.com",
        )

    github_plugin = github_app.github
    github_plugin.get_installation_access_token = AsyncMock(return_value="install-token")
    github_plugin.fetch_installation_repositories = AsyncMock(
        return_value=[_repo(1001, "app", "acme/app")]
    )
    github_plugin.ensure_repo_labels = AsyncMock(side_effect=RuntimeError("boom"))
    github_plugin.fetch_repository_pull_requests = AsyncMock(return_value=[])
    github_plugin.fetch_repository_issues = AsyncMock(return_value=[])

    # Must not raise — the label helper swallows per-repo failures.
    await sync_installation_repositories(GitHubSyncInstallationMessage(installation_id="123"))
