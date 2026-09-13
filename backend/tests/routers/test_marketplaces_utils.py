"""Tests for marketplace utils (provider detection, parsing, sanitize, resolve)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_org, db_create_workspace
from api.routers.marketplaces.schemas import (
    GitHubLocator,
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
)
from api.routers.marketplaces.utils import (
    get_marketplace_provider,
    gitlab_project_path,
    parse_github_url,
    parse_marketplace_response,
    resolve_plugin_clone_target,
    resolve_plugin_specs_for_org,
    sanitize_frontmatter_text,
)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def test_get_provider_github():
    assert get_marketplace_provider("https://github.com/foo/bar") == "github"


def test_get_provider_gitlab():
    assert get_marketplace_provider("https://gitlab.com/foo/bar") == "gitlab"


def test_parse_github_url_https():
    assert parse_github_url("https://github.com/foo/bar") == GitHubLocator(owner="foo", repo="bar")


def test_parse_github_url_git_suffix():
    assert parse_github_url("https://github.com/foo/bar.git").repo == "bar"


def test_parse_github_url_ssh():
    assert parse_github_url("git@github.com:foo/bar.git") == GitHubLocator(owner="foo", repo="bar")


def test_parse_github_url_tree_ref():
    loc = parse_github_url("https://github.com/foo/bar/tree/main")
    assert loc == GitHubLocator(owner="foo", repo="bar", ref="main")


def test_parse_github_url_non_github():
    assert parse_github_url("https://gitlab.com/foo/bar") is None


def test_gitlab_project_path_basic():
    assert gitlab_project_path("https://gitlab.com/foo/bar") == "foo/bar"


def test_gitlab_project_path_nested():
    assert gitlab_project_path("https://gitlab.com/group/sub/project") == "group/sub/project"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_response_happy_path():
    payload = json.dumps({"name": "X", "plugins": [{"name": "a"}]})
    m = parse_marketplace_response(200, payload, "https://github.com/foo/bar")
    assert m.name == "X"
    assert len(m.plugins) == 1


def test_parse_response_404():
    with pytest.raises(MarketplaceFetchError, match="not found"):
        parse_marketplace_response(404, "", "https://github.com/foo/bar")


def test_parse_response_403():
    with pytest.raises(MarketplaceFetchError, match="access denied"):
        parse_marketplace_response(403, "", "https://github.com/foo/bar")


def test_parse_response_invalid_json():
    with pytest.raises(MarketplaceFetchError, match="not valid JSON"):
        parse_marketplace_response(200, "nope", "https://github.com/foo/bar")


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def test_resolve_none_source():
    assert resolve_plugin_clone_target(None, "https://github.com/a/b") == (
        "https://github.com/a/b",
        None,
    )


def test_resolve_string_source():
    assert resolve_plugin_clone_target("plugins/alpha", "https://github.com/a/b") == (
        "https://github.com/a/b",
        "plugins/alpha",
    )


def test_resolve_github_object_source():
    source = {"type": "github", "repo": "other/repo", "path": "pkg"}
    assert resolve_plugin_clone_target(source, "https://github.com/a/b") == (
        "https://github.com/other/repo",
        "pkg",
    )


@pytest.mark.parametrize("source", ["./", ".", "/"])
def test_resolve_root_source_normalizes_to_none(source):
    """A plain ``.strip("/")`` leaves a truthy ``"."`` for ``"./"``/``"."``,
    which downstream gets treated as a real subdirectory name instead of
    "this repo's root" — this is the bug that made every plugin in a
    shared-source marketplace (source: "./" on every entry) unresolvable."""
    assert resolve_plugin_clone_target(source, "https://github.com/a/b") == (
        "https://github.com/a/b",
        None,
    )


def test_resolve_github_object_root_source_normalizes_to_none():
    source = {"type": "github", "repo": "other/repo", "path": "./"}
    assert resolve_plugin_clone_target(source, "https://github.com/a/b") == (
        "https://github.com/other/repo",
        None,
    )


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_sanitize_strips_allowed_tools():
    text = "---\nname: foo\nallowed-tools: Bash\ndescription: hello\n---\nbody\n"
    cleaned = sanitize_frontmatter_text(text)
    assert "allowed-tools" not in cleaned
    assert "name: foo" in cleaned


def test_sanitize_strips_hooks():
    text = "---\nname: foo\nhooks:\n  preToolUse: evil.sh\ndescription: bar\n---\n"
    cleaned = sanitize_frontmatter_text(text)
    assert "hooks" not in cleaned
    assert "description: bar" in cleaned


def test_sanitize_strips_bang():
    assert "rm -rf" not in sanitize_frontmatter_text("Hello !`rm -rf /` world")


def test_sanitize_passthrough():
    text = "just a body"
    assert sanitize_frontmatter_text(text) == text


# ---------------------------------------------------------------------------
# Dispatch resolver
# ---------------------------------------------------------------------------


def _make_org(db):
    ws = db_create_workspace(db=db, name="ws", slug="ws")
    org = db_create_org(
        db=db,
        workspace_id=ws.id,
        name="acme",
        external_org_id="acme-1",
        installation_id="inst-acme",
        provider="github",
        base_url="https://github.com",
    )
    return org.id


@pytest.mark.asyncio
async def test_resolve_empty(db_session):
    org_id = _make_org(db_session)
    assert await resolve_plugin_specs_for_org(db_session, org_id) == []


@pytest.mark.asyncio
async def test_resolve_basic(db_session):
    from api.database import db_create_marketplace, db_create_marketplace_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    db_create_marketplace_install(
        db_session,
        org_id=org_id,
        marketplace_id=m.id,
        plugin_name="alpha",
        display_name="alpha",
    )

    manifest = MarketplaceManifest(
        name="m", plugins=[MarketplacePlugin(name="alpha", source="plugins/alpha")]
    )
    with (
        patch(
            "api.routers.marketplaces.utils._fetch_marketplace_file",
            new=AsyncMock(return_value=(200, manifest.model_dump_json())),
        ),
        patch(
            "api.routers.marketplaces.utils.parse_marketplace_response",
            return_value=manifest,
        ),
    ):
        specs = await resolve_plugin_specs_for_org(db_session, org_id)

    assert len(specs) == 1
    assert specs[0].git_url == "https://github.com/acme/m"
    assert specs[0].plugin_subpath == "plugins/alpha"


@pytest.mark.asyncio
async def test_resolve_carries_skills_allowlist_for_shared_source(db_session):
    """A marketplace where several plugins share one source root (``"./"``)
    and are told apart only by a ``skills`` allowlist — the layout that used
    to resolve every entry to the same root with no way to tell them apart."""
    from api.database import db_create_marketplace, db_create_marketplace_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    db_create_marketplace_install(
        db_session,
        org_id=org_id,
        marketplace_id=m.id,
        plugin_name="handbook",
        display_name="handbook",
    )

    manifest = MarketplaceManifest(
        name="m",
        plugins=[MarketplacePlugin(name="handbook", source="./", skills=["./skills/handbook"])],
    )
    with (
        patch(
            "api.routers.marketplaces.utils._fetch_marketplace_file",
            new=AsyncMock(return_value=(200, manifest.model_dump_json())),
        ),
        patch(
            "api.routers.marketplaces.utils.parse_marketplace_response",
            return_value=manifest,
        ),
    ):
        specs = await resolve_plugin_specs_for_org(db_session, org_id)

    assert len(specs) == 1
    assert specs[0].plugin_subpath is None
    assert specs[0].skills == ["./skills/handbook"]


@pytest.mark.asyncio
async def test_resolve_workflow_filter(db_session):
    from api.database import db_create_marketplace, db_create_marketplace_install, db_update_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    install = db_create_marketplace_install(
        db_session,
        org_id=org_id,
        marketplace_id=m.id,
        plugin_name="p",
        display_name="p",
    )
    db_update_install(db_session, install, enabled_workflows=["code_review"])

    manifest = MarketplaceManifest(
        name="m", plugins=[MarketplacePlugin(name="p", source="plugins/p")]
    )
    with (
        patch(
            "api.routers.marketplaces.utils._fetch_marketplace_file",
            new=AsyncMock(return_value=(200, manifest.model_dump_json())),
        ),
        patch(
            "api.routers.marketplaces.utils.parse_marketplace_response",
            return_value=manifest,
        ),
    ):
        assert await resolve_plugin_specs_for_org(db_session, org_id, workflow="fix") == []
        specs = await resolve_plugin_specs_for_org(db_session, org_id, workflow="code_review")
        assert len(specs) == 1


@pytest.mark.asyncio
async def test_resolve_skips_removed_plugin(db_session):
    from api.database import db_create_marketplace, db_create_marketplace_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    db_create_marketplace_install(
        db_session,
        org_id=org_id,
        marketplace_id=m.id,
        plugin_name="gone",
        display_name="gone",
    )

    manifest = MarketplaceManifest(name="m", plugins=[])
    with (
        patch(
            "api.routers.marketplaces.utils._fetch_marketplace_file",
            new=AsyncMock(return_value=(200, manifest.model_dump_json())),
        ),
        patch(
            "api.routers.marketplaces.utils.parse_marketplace_response",
            return_value=manifest,
        ),
    ):
        assert await resolve_plugin_specs_for_org(db_session, org_id) == []


@pytest.mark.asyncio
async def test_resolve_skips_unreachable(db_session):
    from api.database import db_create_marketplace, db_create_marketplace_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    db_create_marketplace_install(
        db_session,
        org_id=org_id,
        marketplace_id=m.id,
        plugin_name="p",
        display_name="p",
    )

    with patch(
        "api.routers.marketplaces.utils._fetch_marketplace_file",
        new=AsyncMock(return_value=(404, "")),
    ):
        assert await resolve_plugin_specs_for_org(db_session, org_id) == []
