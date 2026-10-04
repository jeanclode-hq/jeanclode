"""Skill sources: parsing what users paste, reading repos, discovering SKILL.md folders.

The GitHub and GitLab plugins are replaced by an in-memory repo, so nothing
here touches the network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers.marketplaces import sources
from api.routers.marketplaces.schemas import MarketplaceFetchError, RepoAuth, RepoLocator
from api.routers.marketplaces.sources import (
    InvalidSourceError,
    find_skill_dirs,
    load_source_manifest,
    parse_repo_locator,
    skill_description,
    skill_names,
)
from tests.utils.fake_repos import FakeRepos, skill_md


def _gh(path, ref=None, subpath=None):
    return RepoLocator(
        provider="github", host="github.com", project_path=path, ref=ref, subpath=subpath
    )


def _gl(path, ref=None, subpath=None, host="gitlab.example.org"):
    return RepoLocator(provider="gitlab", host=host, project_path=path, ref=ref, subpath=subpath)


# ---------------------------------------------------------------------------
# parse_repo_locator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # npx-style GitHub shorthand
        ("anthropics/skills", _gh("anthropics/skills")),
        ("  vercel-labs/agent-skills  ", _gh("vercel-labs/agent-skills")),
        ("my-org/repo.name", _gh("my-org/repo.name")),
        # GitHub URLs
        ("https://github.com/o/r", _gh("o/r")),
        ("https://github.com/o/r/", _gh("o/r")),
        ("https://github.com/o/r.git", _gh("o/r")),
        ("https://www.github.com/o/r", _gh("o/r")),
        ("https://github.com/o/r/tree/main", _gh("o/r", "main")),
        ("https://github.com/o/r/tree/HEAD", _gh("o/r")),
        ("https://github.com/o/r/tree/v1.2/skills", _gh("o/r", "v1.2", "skills")),
        ("https://github.com/o/r/tree/main/skills/pdf/", _gh("o/r", "main", "skills/pdf")),
        ("https://github.com/o/r/blob/main/skills/pdf/SKILL.md", _gh("o/r", "main", "skills/pdf")),
        ("https://github.com/o/r/blob/main/SKILL.md", _gh("o/r", "main")),
        ("https://github.com/o/r/blob/v2/.claude-plugin/marketplace.json", _gh("o/r", "v2")),
        ("github.com/o/r", _gh("o/r")),
        ("git@github.com:o/r.git", _gh("o/r")),
        ("ssh://git@github.com/o/r.git", _gh("o/r")),
        # GitLab URLs, self-hosted and nested groups
        ("https://gitlab.example.org/g/p", _gl("g/p")),
        ("https://gitlab.example.org/g/sub/p.git", _gl("g/sub/p")),
        ("https://GitLab.Example.org/g/p", _gl("g/p")),
        ("https://gitlab.example.org/g/sub/p/-/tree/v1", _gl("g/sub/p", "v1")),
        (
            "https://gitlab.example.org/g/p/-/tree/main/.claude/skills",
            _gl("g/p", "main", ".claude/skills"),
        ),
        (
            "https://gitlab.example.org/g/p/-/blob/6.47.1/.claude/skills/happily/SKILL.md",
            _gl("g/p", "6.47.1", ".claude/skills/happily"),
        ),
        ("https://gitlab.example.org:8443/g/p", _gl("g/p", host="gitlab.example.org:8443")),
        ("gitlab.example.org/g/sub/p", _gl("g/sub/p")),
        ("git@gitlab.example.org:g/sub/p.git", _gl("g/sub/p")),
        ("ssh://git@gitlab.example.org/g/p.git", _gl("g/p")),
        ("https://gitlab.com/g/p", _gl("g/p", host="gitlab.com")),
    ],
)
def test_parse_repo_locator(raw, expected):
    assert parse_repo_locator(raw) == expected


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("owner", "not a GitHub"),
        ("./skills", "not a GitHub"),
        ("../skills", "not a GitHub"),
        ("skills.zip", "missing the group"),
        ("/abs/path", "not a GitHub"),
        ("http://github.com/o/r", "not a GitHub"),
        ("file:///tmp/repo", "not a GitHub"),
        ("ftp://host/o/r", "not a GitHub"),
        ("https://github.com/o", "missing the repository"),
        ("https://github.com/o/r/issues/3", "not a repository, tree or blob"),
        ("https://github.com/o/r/tree", "not a repository, tree or blob"),
        ("https://github.com/o/r/blob/main/README.md", "links a file"),
        ("https://github.com/o/r/blob/main", "doesn't name a file"),
        ("https://github.com/o/r/tree/main/../x", "'..'"),
        ("https://github.com/o/r?tab=readme", "query or fragment"),
        ("https://github.com/o/r#readme", "query or fragment"),
        ("https://gitlab.example.org/g", "missing the group"),
        ("https://gitlab.example.org/g/p/-/merge_requests/3", "not a repository, tree or blob"),
        ("https://gitlab.example.org/g/p/-/tree", "not a repository, tree or blob"),
    ],
)
def test_parse_repo_locator_rejects(raw, match):
    with pytest.raises(InvalidSourceError, match=match):
        parse_repo_locator(raw)


@pytest.mark.parametrize(
    "loc",
    [
        _gh("o/r"),
        _gh("o/r", "main"),
        _gh("o/r", None, "skills/pdf"),
        _gh("o/r", "v1", "skills"),
        _gl("g/sub/p"),
        _gl("g/p", "v1"),
        _gl("g/p", None, ".claude/skills"),
        _gl("g/p", "6.47.1", ".claude/skills/happily"),
    ],
)
def test_canonical_url_round_trips(loc):
    assert parse_repo_locator(loc.canonical_url) == loc


def test_canonical_url_shapes():
    assert _gh("o/r").canonical_url == "https://github.com/o/r"
    assert _gh("o/r", None, "s").canonical_url == "https://github.com/o/r/tree/HEAD/s"
    assert _gl("g/p", "v1").canonical_url == "https://gitlab.example.org/g/p/-/tree/v1"
    assert _gl("g/p").clone_url == "https://gitlab.example.org/g/p"
    assert _gl("g/sub/p").name == "p"


# ---------------------------------------------------------------------------
# find_skill_dirs / skill_names / skill_description
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "subpath", "expected"),
    [
        ([], None, []),
        (["README.md", "src/main.py"], None, []),
        (["SKILL.md", "scripts/run.sh"], None, ["."]),
        # The repo itself is the skill: nothing below it is a separate one.
        (["SKILL.md", "skills/a/SKILL.md"], None, ["."]),
        (
            ["skills/a/SKILL.md", "skills/b/SKILL.md", "skills/b/ref.md"],
            None,
            ["skills/a", "skills/b"],
        ),
        ([".claude/skills/happily/SKILL.md"], None, [".claude/skills/happily"]),
        (
            [".agents/skills/x/SKILL.md", "skills/y/SKILL.md"],
            None,
            [".agents/skills/x", "skills/y"],
        ),
        # A SKILL.md inside another skill belongs to it.
        (["skills/a/SKILL.md", "skills/a/examples/SKILL.md"], None, ["skills/a"]),
        (["node_modules/pkg/SKILL.md", ".git/SKILL.md", "skills/a/SKILL.md"], None, ["skills/a"]),
        (["skills/a/skill.md", "skills/b/SKILL.md.bak"], None, []),
        (["skills/a/SKILL.md", "other/b/SKILL.md"], "skills", ["skills/a"]),
        # A prefix match on the folder name is not "inside" it.
        (["skills-old/a/SKILL.md", "skills/b/SKILL.md"], "skills", ["skills/b"]),
        (["skills/a/SKILL.md", "skills/a/sub/SKILL.md"], "skills/a", ["skills/a"]),
        (["skills/a/SKILL.md"], "skills/missing", []),
    ],
)
def test_find_skill_dirs(paths, subpath, expected):
    assert find_skill_dirs(paths, subpath) == expected


def test_skill_names_use_folder_names_and_fall_back_to_paths_on_collision():
    assert skill_names(["skills/a", ".claude/skills/b"], "repo") == {
        "skills/a": "a",
        ".claude/skills/b": "b",
    }
    assert skill_names(["skills/a", "legacy/a", "skills/b"], "repo") == {
        "skills/a": "skills/a",
        "legacy/a": "legacy/a",
        "skills/b": "b",
    }
    assert skill_names(["."], "happily") == {".": "happily"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nname: a\ndescription: Does A things\n---\nbody", "Does A things"),
        ("---\ndescription: 'quoted'\n---\n", "quoted"),
        ('---\ndescription: "double"\n---\n', "double"),
        (
            "---\ndescription: >\n  folded over\n  two lines\nname: x\n---\n",
            "folded over two lines",
        ),
        ("---\ndescription: |-\n  literal\n---\n", "literal"),
        ("---\nname: a\n---\nbody", None),
        ("---\ndescription:\n---\n", None),
        ("no frontmatter at all", None),
        ("---\ndescription: unterminated", None),
    ],
)
def test_skill_description(text, expected):
    assert skill_description(text) == expected


def test_skill_description_is_trimmed():
    desc = skill_description("---\ndescription: " + "x" * 1000 + "\n---\n")
    assert desc is not None
    assert len(desc) == 300
    assert desc.endswith("…")


# ---------------------------------------------------------------------------
# A fake app whose GitHub and GitLab plugins serve one in-memory repo
# ---------------------------------------------------------------------------


@pytest.fixture
def repos():
    fake = FakeRepos()
    sources._described_cache.clear()
    with patch("api.routers.marketplaces.sources.get_current_app", return_value=fake):
        yield fake
    sources._described_cache.clear()


_skill = skill_md


GL_AUTH = RepoAuth(provider="gitlab", token="glpat", base_url="https://gitlab.example.org")
GH_AUTH = RepoAuth(provider="github", token="ghs", base_url="https://github.com")


# ---------------------------------------------------------------------------
# load_source_manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marketplace_json_wins_when_present(repos):
    repos.add(
        "github",
        "o/r",
        {
            ".claude-plugin/marketplace.json": json.dumps(
                {"name": "mkt", "plugins": [{"name": "p", "source": "./p"}]}
            ),
            "skills/a/SKILL.md": _skill("a"),
        },
    )

    manifest = await load_source_manifest("https://github.com/o/r")

    assert (manifest.name, manifest.kind) == ("mkt", "marketplace")
    assert [p.name for p in manifest.plugins] == ["p"]
    repos.github.list_repo_blob_paths.assert_not_awaited()


@pytest.mark.asyncio
async def test_marketplace_json_is_read_at_the_url_ref(repos):
    """GitLab used to read HEAD whatever ref the marketplace URL named."""
    repos.add(
        "gitlab",
        "numberly/skills",
        {".claude-plugin/marketplace.json": json.dumps({"name": "v2", "plugins": []})},
        ref="v2",
    )

    manifest = await load_source_manifest(
        "https://gitlab.example.org/numberly/skills/-/tree/v2", auth=GL_AUTH
    )

    assert manifest.name == "v2"
    assert repos.gitlab.fetch_repo_file_text.await_args.kwargs["ref"] == "v2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_unreadable_marketplace_json_is_an_error_not_a_skill_scan(repos, status):
    repos.github.fetch_repo_file_text = AsyncMock(return_value=(status, ""))
    with pytest.raises(MarketplaceFetchError):
        await load_source_manifest("o/r")
    repos.github.list_repo_blob_paths.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_marketplace_json_is_an_error(repos):
    repos.add("github", "o/r", {".claude-plugin/marketplace.json": "{nope"})
    with pytest.raises(MarketplaceFetchError, match="not valid JSON"):
        await load_source_manifest("o/r")


@pytest.mark.asyncio
async def test_repo_without_marketplace_becomes_one_plugin_per_skill(repos):
    repos.add(
        "github",
        "anthropics/skills",
        {
            "README.md": "",
            "skills/pdf/SKILL.md": _skill("pdf", "Read PDFs"),
            "skills/pdf/scripts/x.py": "",
            "skills/xlsx/SKILL.md": _skill("xlsx"),
        },
    )

    manifest = await load_source_manifest("anthropics/skills")

    assert manifest.kind == "skills"
    assert manifest.name == "anthropics/skills"
    assert [(p.name, p.source, p.skills, p.description) for p in manifest.plugins] == [
        ("pdf", "./", ["./skills/pdf"], None),
        ("xlsx", "./", ["./skills/xlsx"], None),
    ]


@pytest.mark.asyncio
async def test_single_skill_repo(repos):
    repos.add("gitlab", "guild-backend/happily-skill", {"SKILL.md": _skill("happily")})

    manifest = await load_source_manifest(
        "https://gitlab.example.org/guild-backend/happily-skill", auth=GL_AUTH
    )

    assert [(p.name, p.skills) for p in manifest.plugins] == [("happily-skill", ["."])]


@pytest.mark.asyncio
async def test_folder_url_skips_the_marketplace_and_scans_that_folder(repos):
    repos.add(
        "gitlab",
        "guild-backend/happily",
        {
            ".claude-plugin/marketplace.json": json.dumps({"name": "ignored", "plugins": []}),
            ".claude/skills/happily/SKILL.md": _skill("happily"),
            "skills/other/SKILL.md": _skill("other"),
        },
        ref="6.47.1",
    )

    manifest = await load_source_manifest(
        "https://gitlab.example.org/guild-backend/happily/-/tree/6.47.1/.claude/skills",
        auth=GL_AUTH,
    )

    assert manifest.name == "guild-backend/happily/.claude/skills"
    assert [(p.name, p.skills) for p in manifest.plugins] == [
        ("happily", ["./.claude/skills/happily"])
    ]
    repos.gitlab.fetch_repo_file_text.assert_not_awaited()
    tree = repos.gitlab.list_repo_blob_paths.await_args.kwargs
    assert (tree["ref"], tree["path"]) == ("6.47.1", ".claude/skills")


@pytest.mark.asyncio
async def test_skill_md_url_targets_one_skill(repos):
    repos.add(
        "github",
        "o/r",
        {"skills/a/SKILL.md": _skill("a"), "skills/b/SKILL.md": _skill("b")},
        ref="main",
    )

    manifest = await load_source_manifest("https://github.com/o/r/blob/main/skills/a/SKILL.md")

    assert [p.name for p in manifest.plugins] == ["a"]


@pytest.mark.asyncio
async def test_no_skills_is_a_clear_error(repos):
    repos.add("github", "o/r", {"README.md": "", "src/app.py": ""})
    with pytest.raises(
        MarketplaceFetchError, match=r"no \.claude-plugin/marketplace\.json and no SKILL\.md"
    ):
        await load_source_manifest("o/r")


@pytest.mark.asyncio
async def test_no_skills_in_a_truncated_listing_says_to_narrow_it(repos):
    repos.add("github", "o/r", {"README.md": ""})
    repos.truncated = True
    with pytest.raises(MarketplaceFetchError, match="cut short"):
        await load_source_manifest("o/r")


@pytest.mark.asyncio
async def test_missing_repo_is_a_clear_error(repos):
    with pytest.raises(MarketplaceFetchError, match=r"can't read .*HTTP 404"):
        await load_source_manifest("o/missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_forbidden_listing_names_the_token(repos, status):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a")})
    repos.status[("github", "tree")] = status
    with pytest.raises(MarketplaceFetchError, match="token"):
        await load_source_manifest("o/r")


@pytest.mark.asyncio
async def test_listing_server_error(repos):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a")})
    repos.status[("github", "tree")] = 502
    with pytest.raises(MarketplaceFetchError, match="HTTP 502"):
        await load_source_manifest("o/r")


@pytest.mark.asyncio
async def test_skill_count_is_capped(repos, monkeypatch):
    monkeypatch.setattr(sources, "MAX_SKILLS", 3)
    repos.add("github", "o/r", {f"skills/s{i}/SKILL.md": _skill(f"s{i}") for i in range(10)})

    manifest = await load_source_manifest("o/r")

    assert len(manifest.plugins) == 3


@pytest.mark.asyncio
async def test_describe_reads_each_skill_md(repos):
    repos.add(
        "github",
        "o/r",
        {"skills/a/SKILL.md": _skill("a", "Alpha"), "skills/b/SKILL.md": "no frontmatter"},
    )

    manifest = await load_source_manifest("o/r", describe=True)

    assert {p.name: p.description for p in manifest.plugins} == {"a": "Alpha", "b": None}


@pytest.mark.asyncio
async def test_describe_survives_a_failing_read(repos):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a", "Alpha")})
    original = repos.github.fetch_repo_file_text.side_effect

    async def flaky(owner, repo, file_path, **kw):
        if file_path.endswith("SKILL.md"):
            raise RuntimeError("connection reset")
        return await original(owner, repo, file_path, **kw)

    repos.github.fetch_repo_file_text = AsyncMock(side_effect=flaky)

    manifest = await load_source_manifest("o/r", describe=True)

    assert [(p.name, p.description) for p in manifest.plugins] == [("a", None)]


@pytest.mark.asyncio
async def test_describe_is_capped(repos, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DESCRIBED_SKILLS", 2)
    repos.add("github", "o/r", {f"skills/s{i}/SKILL.md": _skill(f"s{i}") for i in range(5)})

    await load_source_manifest("o/r", describe=True)

    reads = [c.args[2] for c in repos.github.fetch_repo_file_text.await_args_list]
    assert sum(r.endswith("SKILL.md") for r in reads) == 2


@pytest.mark.asyncio
async def test_described_manifest_is_cached_per_token(repos):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a", "Alpha")})

    first = await load_source_manifest("o/r", auth=GH_AUTH, describe=True)
    calls = repos.github.list_repo_blob_paths.await_count
    again = await load_source_manifest("o/r", auth=GH_AUTH, describe=True)
    assert again == first
    assert repos.github.list_repo_blob_paths.await_count == calls

    await load_source_manifest("o/r", auth=None, describe=True)
    assert repos.github.list_repo_blob_paths.await_count == calls + 1


@pytest.mark.asyncio
async def test_described_cache_expires(repos, monkeypatch):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a")})
    now = [1000.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: now[0])

    await load_source_manifest("o/r", describe=True)
    now[0] += sources.DESCRIBED_CACHE_TTL_SECONDS + 1
    await load_source_manifest("o/r", describe=True)

    assert repos.github.list_repo_blob_paths.await_count == 2


@pytest.mark.asyncio
async def test_dispatch_path_is_never_cached_nor_described(repos):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a", "Alpha")})

    await load_source_manifest("o/r")
    await load_source_manifest("o/r")

    assert repos.github.list_repo_blob_paths.await_count == 2
    reads = [c.args[2] for c in repos.github.fetch_repo_file_text.await_args_list]
    assert not any(r.endswith("SKILL.md") for r in reads)


@pytest.mark.asyncio
async def test_invalid_url_raises_invalid_source(repos):
    with pytest.raises(InvalidSourceError):
        await load_source_manifest("not a url")


# ---------------------------------------------------------------------------
# Which host gets which token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_token_only_on_its_own_host(repos):
    repos.add("gitlab", "g/p", {"skills/a/SKILL.md": _skill("a")})

    await load_source_manifest("https://gitlab.example.org/g/p", auth=GL_AUTH)

    for call in (
        repos.gitlab.fetch_repo_file_text.await_args,
        repos.gitlab.list_repo_blob_paths.await_args,
    ):
        assert call.kwargs["auth_token"] == "glpat"
        assert call.kwargs["provider_url"] == "https://gitlab.example.org"


@pytest.mark.asyncio
async def test_gitlab_on_the_configured_instance_is_read_without_another_orgs_token(repos):
    """A GitHub org connecting a repo on jeanclode's GitLab instance reads it anonymously."""
    repos.add("gitlab", "g/p", {"skills/a/SKILL.md": _skill("a")})

    await load_source_manifest("https://gitlab.example.org/g/p", auth=GH_AUTH)

    assert repos.gitlab.list_repo_blob_paths.await_args.kwargs["auth_token"] is None


@pytest.mark.asyncio
async def test_gitlab_org_token_never_goes_to_another_gitlab_host(repos):
    other = RepoAuth(provider="gitlab", token="glpat", base_url="https://gitlab.other.org")
    repos.add("gitlab", "g/p", {"skills/a/SKILL.md": _skill("a")})

    await load_source_manifest("https://gitlab.example.org/g/p", auth=other)

    assert repos.gitlab.list_repo_blob_paths.await_args.kwargs["auth_token"] is None


@pytest.mark.asyncio
async def test_unknown_gitlab_host_is_refused_before_any_request(repos):
    with pytest.raises(MarketplaceFetchError, match="not a GitLab instance connected"):
        await load_source_manifest("https://git.attacker.example/g/p", auth=GL_AUTH)
    repos.gitlab.fetch_repo_file_text.assert_not_awaited()
    repos.gitlab.list_repo_blob_paths.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_gitlab_host_is_allowed_even_when_not_the_configured_instance(repos):
    repos.gitlab.get_effective_instance_url = MagicMock(return_value="https://gitlab.com")
    repos.add("gitlab", "g/p", {"skills/a/SKILL.md": _skill("a")})

    await load_source_manifest("https://gitlab.example.org/g/p", auth=GL_AUTH)

    assert repos.gitlab.list_repo_blob_paths.await_args.kwargs["provider_url"] == (
        "https://gitlab.example.org"
    )


@pytest.mark.asyncio
async def test_github_token_only_for_github_orgs(repos):
    repos.add("github", "o/r", {"skills/a/SKILL.md": _skill("a")})

    await load_source_manifest("o/r", auth=GH_AUTH)
    assert repos.github.list_repo_blob_paths.await_args.kwargs["auth_token"] == "ghs"

    await load_source_manifest("o/r", auth=GL_AUTH)
    assert repos.github.list_repo_blob_paths.await_args.kwargs["auth_token"] is None


@pytest.mark.asyncio
async def test_disabled_provider_plugin_is_an_error(repos):
    repos.github = None
    with pytest.raises(MarketplaceFetchError, match="GitHub plugin is not enabled"):
        await load_source_manifest("o/r")
