"""Tests for GitHubPlugin.ensure_repo_labels.

GitHub has no org-level label, so this runs per-repo — list what's there
(paginated), create only what's missing, never raise.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.plugins.github.dispatch import LABEL_RESOLVE, LABEL_REVIEW, LABEL_SUMMARY
from api.plugins.github.plugin import GitHubPlugin


def _response(status_code: int, json_body=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = text
    return resp


@pytest.fixture
def github_plugin():
    from api.plugins.github.config import GitHubPluginConfig

    plugin = GitHubPlugin(GitHubPluginConfig())
    plugin._http = AsyncMock()
    return plugin


@pytest.mark.asyncio
async def test_creates_only_missing_labels(github_plugin):
    """One of the three labels already exists — only the other two get created."""
    github_plugin.http.get.side_effect = [
        _response(200, [{"name": LABEL_REVIEW}]),
        _response(200, []),
    ]
    github_plugin.http.post.return_value = _response(201)

    await github_plugin.ensure_repo_labels("token", "acme/app")

    assert github_plugin.http.post.call_count == 2
    for call in github_plugin.http.post.call_args_list:
        assert call.args[0] == "/repos/acme/app/labels"
    created_names = {call.kwargs["json"]["name"] for call in github_plugin.http.post.call_args_list}
    assert created_names == {LABEL_SUMMARY, LABEL_RESOLVE}


@pytest.mark.asyncio
async def test_all_labels_already_exist_creates_nothing(github_plugin):
    github_plugin.http.get.side_effect = [
        _response(200, [{"name": LABEL_REVIEW}, {"name": LABEL_SUMMARY}, {"name": LABEL_RESOLVE}]),
        _response(200, []),
    ]

    await github_plugin.ensure_repo_labels("token", "acme/app")

    github_plugin.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_paginates_through_existing_labels(github_plugin):
    """A repo with >100 labels shouldn't get false "missing" positives past page 1."""
    page_1 = [{"name": f"other-{i}"} for i in range(100)]
    page_2 = [{"name": LABEL_REVIEW}, {"name": LABEL_SUMMARY}, {"name": LABEL_RESOLVE}]
    github_plugin.http.get.side_effect = [
        _response(200, page_1),
        _response(200, page_2),
        _response(200, []),
    ]

    await github_plugin.ensure_repo_labels("token", "acme/app")

    assert github_plugin.http.get.call_count == 3
    github_plugin.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_create_race_is_tolerated(github_plugin):
    """A 422 on create (label created by a racing sync) doesn't raise."""
    github_plugin.http.get.return_value = _response(200, [])
    github_plugin.http.post.return_value = _response(422, text="already_exists")

    await github_plugin.ensure_repo_labels("token", "acme/app")  # must not raise


@pytest.mark.asyncio
async def test_list_failure_is_best_effort(github_plugin):
    """A failed list call logs and returns rather than raising or creating blind."""
    github_plugin.http.get.return_value = _response(500, text="server error")

    await github_plugin.ensure_repo_labels("token", "acme/app")  # must not raise

    github_plugin.http.post.assert_not_called()
