"""Dispatch-side resolution of third-party plugins into the container env."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.plugins.container.utils import (
    _get_dispatch_git_token,
    _get_dispatch_repo_auth,
    resolve_third_party_plugins_env_for_org,
)
from api.routers.marketplaces.schemas import RepoAuth, ResolvedPluginSpec

_APP = "api.plugins.container.utils.get_current_app"
_ORG = "api.database.organization.db_get_org_by_id"


def _app(*, github_token: str | None = "ghs", decrypt=lambda v: f"plain:{v}"):
    db = MagicMock()
    db.session.return_value.__enter__.return_value = MagicMock()
    db.decrypt.side_effect = decrypt
    db.run_in_session = AsyncMock(side_effect=lambda fn: fn(MagicMock()))
    github = SimpleNamespace(get_installation_access_token=AsyncMock(return_value=github_token))
    return SimpleNamespace(database=db, github=github)


def _org(**kw):
    defaults = {
        "provider": "gitlab",
        "installation_id": None,
        "auth_token_encrypted": "enc",
        "base_url": "https://gitlab.example.org",
    }
    return SimpleNamespace(**{**defaults, **kw})


@pytest.mark.asyncio
async def test_gitlab_org_auth_carries_its_host():
    with patch(_APP, return_value=_app()), patch(_ORG, return_value=_org()):
        auth = await _get_dispatch_repo_auth(uuid.uuid4())
    assert auth == RepoAuth(
        provider="gitlab", token="plain:enc", base_url="https://gitlab.example.org"
    )


@pytest.mark.asyncio
async def test_github_org_auth_mints_an_installation_token():
    org = _org(provider="github", installation_id="42", auth_token_encrypted=None, base_url=None)
    app = _app()
    with patch(_APP, return_value=app), patch(_ORG, return_value=org):
        auth = await _get_dispatch_repo_auth(uuid.uuid4())
    assert auth == RepoAuth(provider="github", token="ghs", base_url=None)
    app.github.get_installation_access_token.assert_awaited_once_with("42")


@pytest.mark.asyncio
async def test_token_failure_still_returns_the_host_so_public_sources_load():
    def boom(_):
        raise ValueError("bad key")

    with patch(_APP, return_value=_app(decrypt=boom)), patch(_ORG, return_value=_org()):
        auth = await _get_dispatch_repo_auth(uuid.uuid4())
    assert auth == RepoAuth(provider="gitlab", token=None, base_url="https://gitlab.example.org")


@pytest.mark.asyncio
@pytest.mark.parametrize("org", [None, _org(provider="sentry")])
async def test_no_auth_for_missing_or_non_git_org(org):
    with patch(_APP, return_value=_app()), patch(_ORG, return_value=org):
        assert await _get_dispatch_repo_auth(uuid.uuid4()) is None
        assert await _get_dispatch_git_token(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_git_token_helper_still_returns_the_bare_token():
    with patch(_APP, return_value=_app()), patch(_ORG, return_value=_org()):
        assert await _get_dispatch_git_token(uuid.uuid4()) == "plain:enc"


@pytest.mark.asyncio
async def test_env_payload_hosts_and_clone_urls():
    auth = RepoAuth(provider="gitlab", token="t", base_url="https://gitlab.example.org")
    specs = [
        ResolvedPluginSpec(
            git_url="https://gitlab.example.org/platform-team/framework.git",
            ref="6.47.1",
            display_name="framework",
            skills=["./.claude/skills/framework"],
        ),
        ResolvedPluginSpec(git_url="https://github.com/anthropics/skills", skills=["./skills/pdf"]),
        ResolvedPluginSpec(
            git_url="https://github.com/anthropics/skills", skills=["./skills/xlsx"]
        ),
    ]
    resolve = AsyncMock(return_value=specs)
    with (
        patch(_APP, return_value=_app()),
        patch("api.plugins.container.utils._get_dispatch_repo_auth", AsyncMock(return_value=auth)),
        patch("api.routers.marketplaces.utils.read_plugin_install_rows", return_value=["row"]),
        patch("api.routers.marketplaces.utils.resolve_plugin_specs", resolve),
    ):
        resolved = await resolve_third_party_plugins_env_for_org(uuid.uuid4(), workflow="fix")

    assert resolve.await_args.kwargs == {"auth": auth}
    assert resolved.env["JEANCLODE_THIRDPARTY_PLUGINS_ENABLED"] == "1"
    assert json.loads(resolved.env["JEANCLODE_THIRDPARTY_PLUGINS"]) == [
        s.model_dump(exclude_none=True) for s in specs
    ]
    assert resolved.extra_hosts == ["github.com", "gitlab.example.org"]
    assert resolved.git_urls == [
        "https://github.com/anthropics/skills",
        "https://gitlab.example.org/platform-team/framework.git",
    ]


@pytest.mark.asyncio
async def test_no_specs_means_no_env():
    with (
        patch(_APP, return_value=_app()),
        patch("api.plugins.container.utils._get_dispatch_repo_auth", AsyncMock(return_value=None)),
        patch("api.routers.marketplaces.utils.read_plugin_install_rows", return_value=[]),
        patch("api.routers.marketplaces.utils.resolve_plugin_specs", AsyncMock(return_value=[])),
    ):
        resolved = await resolve_third_party_plugins_env_for_org(uuid.uuid4(), workflow="fix")
    assert (resolved.env, resolved.extra_hosts, resolved.git_urls) == ({}, [], [])


@pytest.mark.asyncio
async def test_resolver_crash_never_fails_the_dispatch():
    with (
        patch(_APP, return_value=_app()),
        patch("api.plugins.container.utils._get_dispatch_repo_auth", AsyncMock(return_value=None)),
        patch("api.routers.marketplaces.utils.read_plugin_install_rows", return_value=["row"]),
        patch(
            "api.routers.marketplaces.utils.resolve_plugin_specs",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        resolved = await resolve_third_party_plugins_env_for_org(uuid.uuid4(), workflow="fix")
    assert resolved.env == {}


@pytest.mark.asyncio
async def test_a_subgroup_run_reads_the_connected_orgs_installs():
    subgroup, connected = uuid.uuid4(), uuid.uuid4()
    read = MagicMock(return_value=[])
    with (
        patch(_APP, return_value=_app()),
        patch("api.plugins.container.utils._get_dispatch_repo_auth", AsyncMock(return_value=None)),
        patch("api.database.organization.db_get_connection_org_id", return_value=connected),
        patch("api.routers.marketplaces.utils.read_plugin_install_rows", read),
        patch("api.routers.marketplaces.utils.resolve_plugin_specs", AsyncMock(return_value=[])),
    ):
        await resolve_third_party_plugins_env_for_org(subgroup, workflow="fix")
    assert read.call_args.args[1] == connected


@pytest.mark.asyncio
async def test_a_subgroup_uses_the_connected_orgs_token():
    subgroup, connected = uuid.uuid4(), uuid.uuid4()
    get_org = MagicMock(return_value=_org())
    with (
        patch(_APP, return_value=_app()),
        patch("api.database.organization.db_get_connection_org_id", return_value=connected),
        patch(_ORG, get_org),
    ):
        auth = await _get_dispatch_repo_auth(subgroup)
    assert get_org.call_args.args[1] == connected
    assert auth is not None and auth.token == "plain:enc"
