"""Tests for GitLabPlugin.ensure_group_labels.

Runs once per connected group — GitLab inherits group labels down to every
subgroup and project, so no per-project loop is needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.plugins.gitlab.dispatch import LABEL_RESOLVE, LABEL_REVIEW, LABEL_SUMMARY
from api.plugins.gitlab.plugin import GitLabPlugin


def _response(status_code: int, json_body=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = text
    return resp


@pytest.fixture
def gitlab_plugin():
    from api.plugins.gitlab.config import GitLabPluginConfig

    plugin = GitLabPlugin(GitLabPluginConfig(enabled=True))
    plugin._http = AsyncMock()
    return plugin


@pytest.mark.asyncio
async def test_creates_only_missing_labels(gitlab_plugin):
    gitlab_plugin.http.get.return_value = _response(200, [{"name": LABEL_REVIEW}])
    gitlab_plugin.http.post.return_value = _response(201)

    await gitlab_plugin.ensure_group_labels("token", "500", provider_url="https://gitlab.com")

    assert gitlab_plugin.http.post.call_count == 2
    for call in gitlab_plugin.http.post.call_args_list:
        assert call.args[0] == "https://gitlab.com/api/v4/groups/500/labels"
        assert call.kwargs["json"]["color"].startswith("#")
    created_names = {call.kwargs["json"]["name"] for call in gitlab_plugin.http.post.call_args_list}
    assert created_names == {LABEL_SUMMARY, LABEL_RESOLVE}


@pytest.mark.asyncio
async def test_all_labels_already_exist_creates_nothing(gitlab_plugin):
    gitlab_plugin.http.get.return_value = _response(
        200, [{"name": LABEL_REVIEW}, {"name": LABEL_SUMMARY}, {"name": LABEL_RESOLVE}]
    )

    await gitlab_plugin.ensure_group_labels("token", "500", provider_url="https://gitlab.com")

    gitlab_plugin.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_paginates_through_existing_labels(gitlab_plugin):
    page_1 = [{"name": f"other-{i}"} for i in range(100)]
    page_2 = [{"name": LABEL_REVIEW}, {"name": LABEL_SUMMARY}, {"name": LABEL_RESOLVE}]
    gitlab_plugin.http.get.side_effect = [
        _response(200, page_1),
        _response(200, page_2),
        _response(200, []),
    ]

    await gitlab_plugin.ensure_group_labels("token", "500", provider_url="https://gitlab.com")

    assert gitlab_plugin.http.get.call_count == 3
    gitlab_plugin.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_list_failure_is_best_effort(gitlab_plugin):
    gitlab_plugin.http.get.return_value = _response(500, text="server error")

    await gitlab_plugin.ensure_group_labels(
        "token", "500", provider_url="https://gitlab.com"
    )  # must not raise

    gitlab_plugin.http.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_failure_is_best_effort(gitlab_plugin):
    """A 400 "already taken" (label created by a racing connect) doesn't raise."""
    gitlab_plugin.http.get.return_value = _response(200, [])
    gitlab_plugin.http.post.return_value = _response(400, text="has already been taken")

    await gitlab_plugin.ensure_group_labels(
        "token", "500", provider_url="https://gitlab.com"
    )  # must not raise
