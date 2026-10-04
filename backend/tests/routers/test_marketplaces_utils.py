"""Tests for marketplace utils (source resolution, sanitize, dispatch resolver)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_org, db_create_workspace
from api.routers.marketplaces.schemas import (
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
    PluginCloneTarget,
    RepoAuth,
    UnsupportedPluginSourceError,
)
from api.routers.marketplaces.sources import parse_marketplace_response
from api.routers.marketplaces.utils import (
    PluginInstallRow,
    resolve_plugin_clone_target,
    resolve_plugin_specs,
    resolve_plugin_specs_for_org,
    sanitize_frontmatter_text,
)

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


MARKET = "https://github.com/a/b"


def _target(git_url, subpath=None, ref=None, sha=None):
    return PluginCloneTarget(git_url=git_url, subpath=subpath, ref=ref, sha=sha)


def test_resolve_none_source():
    assert resolve_plugin_clone_target(None, MARKET) == _target(MARKET)


@pytest.mark.parametrize(
    "source", ["plugins/alpha", "./plugins/alpha", {"source": "./plugins/alpha"}]
)
def test_resolve_relative_source(source):
    assert resolve_plugin_clone_target(source, MARKET) == _target(MARKET, "plugins/alpha")


@pytest.mark.parametrize("source", ["./", ".", "/"])
def test_resolve_root_source_normalizes_to_none(source):
    """A truthy ``"."`` left over from ``"./"`` used to be treated as a real
    subdirectory, making every plugin of a shared-source marketplace unresolvable."""
    assert resolve_plugin_clone_target(source, MARKET) == _target(MARKET)


def test_resolve_bare_name_under_plugin_root():
    assert resolve_plugin_clone_target("formatter", MARKET, plugin_root="./plugins") == _target(
        MARKET, "plugins/formatter"
    )


def test_resolve_relative_source_clones_marketplace_at_its_ref():
    """A marketplace connected by its web link can't be cloned as-is."""
    assert resolve_plugin_clone_target(
        "./x", "https://gitlab.example.org/team/skills/-/tree/release-v2"
    ) == _target("https://gitlab.example.org/team/skills", "x", ref="release-v2")
    assert resolve_plugin_clone_target("./x", "https://github.com/a/b/tree/main") == _target(
        "https://github.com/a/b", "x", ref="main"
    )


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ("anthropics/skills", _target("https://github.com/anthropics/skills")),
        (
            "https://github.com/o/r/tree/HEAD/skills",
            _target("https://github.com/o/r"),
        ),
        (
            "https://gitlab.example.org/g/sub/p/-/tree/v1/.claude/skills",
            _target("https://gitlab.example.org/g/sub/p", ref="v1"),
        ),
        ("git@gitlab.example.org:g/p.git", _target("https://gitlab.example.org/g/p")),
    ],
)
def test_resolve_relative_source_against_any_stored_source_url(market, expected):
    """A discovered skill's paths are repo-relative, so the clone is the bare repo."""
    assert resolve_plugin_clone_target("./", market) == expected


def test_resolve_relative_source_against_an_unparseable_marketplace_url():
    with pytest.raises(UnsupportedPluginSourceError):
        resolve_plugin_clone_target("./x", "not a url")


def test_resolve_github_source_claude_code_schema():
    source = {"source": "github", "repo": "other/repo", "ref": "v2.0.0", "sha": "a" * 40}
    assert resolve_plugin_clone_target(source, MARKET) == _target(
        "https://github.com/other/repo", ref="v2.0.0", sha="a" * 40
    )


@pytest.mark.parametrize("path", ["pkg", "./pkg"])
def test_resolve_legacy_github_type_source(path):
    source = {"type": "github", "repo": "other/repo", "path": path}
    assert resolve_plugin_clone_target(source, MARKET) == _target(
        "https://github.com/other/repo", "pkg"
    )


def test_resolve_url_source_on_another_gitlab_project():
    """The acme-skills ``framework`` entry once it moves to its own repo."""
    source = {
        "source": "url",
        "url": "https://gitlab.example.org/platform-team/framework.git",
        "ref": "6.47.1",
    }
    assert resolve_plugin_clone_target(source, MARKET) == _target(
        "https://gitlab.example.org/platform-team/framework.git", ref="6.47.1"
    )


@pytest.mark.parametrize(
    "url",
    ["git@gitlab.example.org:team/x.git", "ssh://git@gitlab.example.org/team/x.git"],
)
def test_resolve_url_source_rewrites_ssh_to_https(url):
    assert resolve_plugin_clone_target({"source": "url", "url": url}, MARKET) == _target(
        "https://gitlab.example.org/team/x.git"
    )


def test_resolve_git_subdir_source():
    source = {
        "source": "git-subdir",
        "url": "https://gitlab.example.org/t/mono.git",
        "path": "tools/fmt",
    }
    assert resolve_plugin_clone_target(source, MARKET) == _target(
        "https://gitlab.example.org/t/mono.git", "tools/fmt"
    )


def test_resolve_git_subdir_github_shorthand():
    source = {"source": "git-subdir", "url": "acme/mono", "path": "tools/fmt", "ref": "main"}
    assert resolve_plugin_clone_target(source, MARKET) == _target(
        "https://github.com/acme/mono", "tools/fmt", ref="main"
    )


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ({"source": "npm", "package": "@acme/fmt"}, "'npm' is not supported"),
        ({"source": "archive", "url": "https://x/y.zip"}, "'archive' is not supported"),
        ({"source": "command", "command": "make"}, "'command' is not supported"),
        ({"repo": "a/b"}, "no 'source' type"),
        ({"source": "github", "repo": "nope"}, "owner/repo"),
        ({"source": "url"}, "needs a 'url'"),
        ({"source": "url", "url": "http://insecure/x.git"}, "https"),
        ({"source": "url", "url": "file:///etc"}, "https"),
        ({"source": "git-subdir", "url": "a/b"}, "needs a 'path'"),
        ({"source": "github", "repo": "a/b", "sha": "abc"}, "40-character"),
        ("./../outside", "'..'"),
        ({"source": "github", "repo": "a/b", "path": "x/../../y"}, "'..'"),
        (42, "string or object"),
    ],
)
def test_resolve_unsupported_source_raises(source, match):
    with pytest.raises(UnsupportedPluginSourceError, match=match):
        resolve_plugin_clone_target(source, MARKET)


def test_manifest_accepts_single_skill_string():
    plugin = MarketplacePlugin(name="p", skills="./skills/p")
    assert plugin.skills == ["./skills/p"]


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


_LOAD = "api.routers.marketplaces.utils.load_source_manifest"


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
    with patch(_LOAD, new=AsyncMock(return_value=manifest)):
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
    with patch(_LOAD, new=AsyncMock(return_value=manifest)):
        specs = await resolve_plugin_specs_for_org(db_session, org_id)

    assert len(specs) == 1
    assert specs[0].plugin_subpath is None
    assert specs[0].skills == ["./skills/handbook"]


async def _resolve_with_manifest(db_session, plugins, *, pinned_ref=None):
    from api.database import db_create_marketplace, db_create_marketplace_install

    org_id = _make_org(db_session)
    m = db_create_marketplace(
        db_session, org_id=org_id, name="m", git_url="https://github.com/acme/m"
    )
    for plugin in plugins:
        db_create_marketplace_install(
            db_session,
            org_id=org_id,
            marketplace_id=m.id,
            plugin_name=plugin.name,
            display_name=plugin.name,
            pinned_ref=pinned_ref,
        )
    manifest = MarketplaceManifest(name="m", plugins=plugins)
    with patch(_LOAD, new=AsyncMock(return_value=manifest)):
        return await resolve_plugin_specs_for_org(db_session, org_id)


@pytest.mark.asyncio
async def test_resolve_url_source_carries_ref_and_skills(db_session):
    specs = await _resolve_with_manifest(
        db_session,
        [
            MarketplacePlugin(
                name="framework",
                source={
                    "source": "url",
                    "url": "https://gitlab.example.org/pt/framework.git",
                    "ref": "6.47.1",
                },
                skills=["./.claude/skills/framework"],
            )
        ],
    )
    assert [s.model_dump(exclude_none=True) for s in specs] == [
        {
            "git_url": "https://gitlab.example.org/pt/framework.git",
            "ref": "6.47.1",
            "display_name": "framework",
            "skills": ["./.claude/skills/framework"],
        }
    ]


@pytest.mark.asyncio
async def test_resolve_install_pin_overrides_manifest_pin(db_session):
    specs = await _resolve_with_manifest(
        db_session,
        [
            MarketplacePlugin(
                name="p", source={"source": "github", "repo": "a/p", "ref": "v1", "sha": "b" * 40}
            )
        ],
        pinned_ref="v2",
    )
    assert (specs[0].ref, specs[0].sha) == ("v2", None)


@pytest.mark.asyncio
async def test_resolve_logs_and_skips_unsupported_source(db_session, caplog):
    specs = await _resolve_with_manifest(
        db_session,
        [
            MarketplacePlugin(name="pkg", source={"source": "npm", "package": "@a/pkg"}),
            MarketplacePlugin(name="ok", source="./ok"),
        ],
    )
    assert [s.plugin_subpath for s in specs] == ["ok"]
    assert "'pkg'" in caplog.text and "'npm' is not supported" in caplog.text


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
    with patch(_LOAD, new=AsyncMock(return_value=manifest)):
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
    with patch(_LOAD, new=AsyncMock(return_value=manifest)):
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

    with patch(_LOAD, new=AsyncMock(side_effect=MarketplaceFetchError("gone"))):
        assert await resolve_plugin_specs_for_org(db_session, org_id) == []


@pytest.mark.asyncio
async def test_resolve_loads_each_source_once_per_dispatch():
    rows = [
        PluginInstallRow(
            plugin_name=n, display_name=n, pinned_ref=None, marketplace_git_url="acme/skills"
        )
        for n in ("pdf", "xlsx")
    ]
    manifest = MarketplaceManifest(
        name="acme/skills",
        kind="skills",
        plugins=[
            MarketplacePlugin(name=n, source="./", skills=[f"./skills/{n}"])
            for n in ("pdf", "xlsx")
        ],
    )
    auth = RepoAuth(provider="github", token="ghs")
    load = AsyncMock(return_value=manifest)

    with patch(_LOAD, new=load):
        specs = await resolve_plugin_specs(rows, auth=auth)

    load.assert_awaited_once_with("acme/skills", auth=auth)
    assert [(s.git_url, s.skills) for s in specs] == [
        ("https://github.com/acme/skills", ["./skills/pdf"]),
        ("https://github.com/acme/skills", ["./skills/xlsx"]),
    ]


@pytest.mark.asyncio
async def test_resolve_logs_an_error_for_an_unreadable_source(caplog):
    rows = [
        PluginInstallRow(
            plugin_name="p", display_name="p", pinned_ref=None, marketplace_git_url="acme/gone"
        )
    ]
    with patch(_LOAD, new=AsyncMock(side_effect=MarketplaceFetchError("HTTP 404"))):
        assert await resolve_plugin_specs(rows) == []
    [record] = [r for r in caplog.records if "acme/gone" in r.getMessage()]
    assert record.levelname == "ERROR"
    assert "HTTP 404" in record.getMessage()
