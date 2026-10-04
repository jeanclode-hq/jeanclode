"""list_repo_blob_paths on the GitHub and GitLab plugins, over a mocked HTTP client."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.plugins.github.config import GitHubPluginConfig
from api.plugins.github.plugin import GitHubPlugin
from api.plugins.gitlab.config import GitLabPluginConfig
from api.plugins.gitlab.plugin import GitLabPlugin, _next_link

GL = "https://gitlab.com"


def _resp(status: int, body, headers: dict | None = None):
    return SimpleNamespace(status_code=status, json=lambda: body, text="", headers=headers or {})


def _github() -> GitHubPlugin:
    plugin = GitHubPlugin(GitHubPluginConfig(enabled=True))
    plugin._http = AsyncMock()
    return plugin


def _gitlab() -> GitLabPlugin:
    plugin = GitLabPlugin(GitLabPluginConfig(enabled=True, instance_url="https://gitlab.com"))
    plugin._http = AsyncMock()
    return plugin


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_lists_blobs_recursively_at_default_branch():
    plugin = _github()
    plugin._http.get = AsyncMock(
        return_value=_resp(
            200,
            {
                "tree": [
                    {"path": "skills", "type": "tree"},
                    {"path": "skills/a/SKILL.md", "type": "blob"},
                    {"path": "sub", "type": "commit"},
                ],
                "truncated": False,
            },
        )
    )

    assert await plugin.list_repo_blob_paths("o", "r") == (200, ["skills/a/SKILL.md"], False)

    call = plugin._http.get.await_args
    assert call.args[0] == "/repos/o/r/git/trees/HEAD"
    assert call.kwargs["params"] == {"recursive": "1"}
    assert "Authorization" not in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_github_quotes_the_ref_and_sends_the_token():
    plugin = _github()
    plugin._http.get = AsyncMock(return_value=_resp(200, {"tree": []}))

    await plugin.list_repo_blob_paths("o", "r", ref="feat/x", auth_token="tok")

    call = plugin._http.get.await_args
    assert call.args[0] == "/repos/o/r/git/trees/feat%2Fx"
    assert call.kwargs["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_github_reports_truncation():
    plugin = _github()
    plugin._http.get = AsyncMock(
        return_value=_resp(200, {"tree": [{"path": "a", "type": "blob"}], "truncated": True})
    )
    assert await plugin.list_repo_blob_paths("o", "r") == (200, ["a"], True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 409, 500])
async def test_github_passes_errors_through(status):
    plugin = _github()
    plugin._http.get = AsyncMock(return_value=_resp(status, {"message": "nope"}))
    assert await plugin.list_repo_blob_paths("o", "r") == (status, [], False)


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_single_page():
    plugin = _gitlab()
    plugin._http.get = AsyncMock(
        return_value=_resp(
            200,
            [{"path": "skills", "type": "tree"}, {"path": "skills/a/SKILL.md", "type": "blob"}],
        )
    )

    result = await plugin.list_repo_blob_paths(
        "grp/sub/proj", provider_url="https://gitlab.example.org"
    )

    assert result == (200, ["skills/a/SKILL.md"], False)
    call = plugin._http.get.await_args
    assert call.args[0] == (
        "https://gitlab.example.org/api/v4/projects/grp%2Fsub%2Fproj/repository/tree"
    )
    assert call.kwargs["params"] == {
        "recursive": "true",
        "per_page": "100",
        "pagination": "keyset",
        "ref": "HEAD",
    }
    assert call.kwargs["headers"] == {}


@pytest.mark.asyncio
async def test_gitlab_passes_ref_path_and_token():
    plugin = _gitlab()
    plugin._http.get = AsyncMock(return_value=_resp(200, []))

    await plugin.list_repo_blob_paths(
        "g/p", ref="v1", path="tools/skills", auth_token="glpat", provider_url=GL
    )

    call = plugin._http.get.await_args
    assert call.args[0].startswith("https://gitlab.com/api/v4/projects/g%2Fp/")
    assert call.kwargs["params"]["ref"] == "v1"
    assert call.kwargs["params"]["path"] == "tools/skills"
    assert call.kwargs["headers"] == {"PRIVATE-TOKEN": "glpat"}


@pytest.mark.asyncio
async def test_gitlab_follows_keyset_pages():
    plugin = _gitlab()
    next_url = "https://gitlab.com/api/v4/projects/1/repository/tree?page_token=abc"
    plugin._http.get = AsyncMock(
        side_effect=[
            _resp(
                200, [{"path": "a/SKILL.md", "type": "blob"}], {"Link": f'<{next_url}>; rel="next"'}
            ),
            _resp(200, [{"path": "b/SKILL.md", "type": "blob"}], {}),
        ]
    )

    assert await plugin.list_repo_blob_paths("g/p", provider_url=GL) == (
        200,
        ["a/SKILL.md", "b/SKILL.md"],
        False,
    )

    second = plugin._http.get.await_args_list[1]
    assert second.args[0] == next_url
    assert second.kwargs["params"] == {}


@pytest.mark.asyncio
async def test_gitlab_stops_at_max_pages_and_says_so():
    plugin = _gitlab()
    looping = _resp(
        200, [{"path": "x", "type": "blob"}], {"Link": '<https://gitlab.com/next>; rel="next"'}
    )
    plugin._http.get = AsyncMock(return_value=looping)

    status, paths, truncated = await plugin.list_repo_blob_paths(
        "g/p", provider_url=GL, max_pages=3
    )

    assert (status, len(paths), truncated) == (200, 3, True)
    assert plugin._http.get.await_count == 3


@pytest.mark.asyncio
async def test_gitlab_error_on_a_later_page_fails_the_listing():
    plugin = _gitlab()
    plugin._http.get = AsyncMock(
        side_effect=[
            _resp(
                200, [{"path": "a", "type": "blob"}], {"Link": '<https://gitlab.com/n>; rel="next"'}
            ),
            _resp(500, {}),
        ]
    )
    assert await plugin.list_repo_blob_paths("g/p", provider_url=GL) == (500, [], False)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_gitlab_passes_errors_through(status):
    plugin = _gitlab()
    plugin._http.get = AsyncMock(return_value=_resp(status, {"message": "nope"}))
    assert await plugin.list_repo_blob_paths("g/p", provider_url=GL) == (status, [], False)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", None),
        ('<https://x/first>; rel="first"', None),
        ('<https://x/next>; rel="next"', "https://x/next"),
        ('<https://x/first>; rel="first", <https://x/next>; rel="next"', "https://x/next"),
    ],
)
def test_next_link(header, expected):
    assert _next_link(header) == expected
