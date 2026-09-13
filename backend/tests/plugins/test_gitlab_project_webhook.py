"""Tests for GitLabPlugin.ensure_project_webhook and webhook-URL resolution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.plugins.gitlab.config import GitLabPluginConfig
from api.plugins.gitlab.plugin import GitLabPlugin

HOOK_URL = "https://jc.example.com/webhooks/gitlab"
_ALL_ON = {
    "issues_events": True,
    "confidential_issues_events": True,
    "merge_requests_events": True,
    "note_events": True,
    "confidential_note_events": True,
}


def _plugin() -> GitLabPlugin:
    plugin = GitLabPlugin(GitLabPluginConfig(enabled=True, instance_url="https://gitlab.com"))
    plugin._http = AsyncMock()
    return plugin


def _resp(status: int, body):
    return SimpleNamespace(status_code=status, json=lambda: body, text="")


def test_get_effective_webhook_url_prefers_explicit_override():
    plugin = GitLabPlugin(GitLabPluginConfig(enabled=True, webhook_url=f"{HOOK_URL}/"))
    assert plugin.get_effective_webhook_url() == HOOK_URL


@pytest.mark.asyncio
async def test_get_effective_webhook_url_derives_from_backend_url(app):
    # test_config.yaml sets options.backend_url = http://localhost:8099
    assert _plugin().get_effective_webhook_url() == "http://localhost:8099/webhooks/gitlab"


@pytest.mark.asyncio
async def test_get_effective_webhook_url_none_without_backend_url(app):
    app.options.backend_url = None
    assert _plugin().get_effective_webhook_url() is None


@pytest.mark.asyncio
async def test_ensure_project_webhook_creates_when_missing():
    plugin = _plugin()
    plugin._http.get = AsyncMock(return_value=_resp(200, []))
    plugin._http.post = AsyncMock(return_value=_resp(201, {"id": 1}))

    result = await plugin.ensure_project_webhook(
        "tok", "55", hook_url=HOOK_URL, secret="s", provider_url="https://gitlab.com"
    )

    assert result == "created"
    body = plugin._http.post.await_args.kwargs["json"]
    assert body["url"] == HOOK_URL
    assert body["token"] == "s"
    assert all(body[flag] for flag in _ALL_ON)
    assert body["push_events"] is False


@pytest.mark.asyncio
async def test_ensure_project_webhook_noop_when_already_correct():
    plugin = _plugin()
    plugin._http.get = AsyncMock(return_value=_resp(200, [{"id": 9, "url": HOOK_URL, **_ALL_ON}]))
    plugin._http.post = AsyncMock()
    plugin._http.put = AsyncMock()

    result = await plugin.ensure_project_webhook(
        "tok", "55", hook_url=HOOK_URL, secret="s", provider_url="https://gitlab.com"
    )

    assert result == "exists"
    plugin._http.post.assert_not_awaited()
    plugin._http.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_project_webhook_updates_a_drifted_hook():
    plugin = _plugin()
    drifted = {"id": 9, "url": HOOK_URL, **_ALL_ON, "note_events": False}
    plugin._http.get = AsyncMock(return_value=_resp(200, [drifted]))
    plugin._http.put = AsyncMock(return_value=_resp(200, {"id": 9}))

    result = await plugin.ensure_project_webhook(
        "tok", "55", hook_url=HOOK_URL, secret="s", provider_url="https://gitlab.com"
    )

    assert result == "updated"
    assert plugin._http.put.await_args.args[0].endswith("/hooks/9")


@pytest.mark.asyncio
async def test_delete_project_webhook_removes_the_matching_hook():
    plugin = _plugin()
    plugin._http.get = AsyncMock(
        return_value=_resp(200, [{"id": 3, "url": "https://other"}, {"id": 7, "url": HOOK_URL}])
    )
    plugin._http.delete = AsyncMock(return_value=_resp(204, {}))

    result = await plugin.delete_project_webhook(
        "tok", "55", hook_url=HOOK_URL, provider_url="https://gitlab.com"
    )

    assert result == "deleted"
    assert plugin._http.delete.await_args.args[0].endswith("/hooks/7")


@pytest.mark.asyncio
async def test_delete_project_webhook_absent_when_not_found():
    plugin = _plugin()
    plugin._http.get = AsyncMock(return_value=_resp(200, [{"id": 3, "url": "https://other"}]))
    plugin._http.delete = AsyncMock()

    result = await plugin.delete_project_webhook(
        "tok", "55", hook_url=HOOK_URL, provider_url="https://gitlab.com"
    )

    assert result == "absent"
    plugin._http.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_project_webhook_absent_when_project_gone():
    plugin = _plugin()
    plugin._http.get = AsyncMock(return_value=_resp(404, {"message": "404 Project Not Found"}))

    result = await plugin.delete_project_webhook(
        "tok", "55", hook_url=HOOK_URL, provider_url="https://gitlab.com"
    )

    assert result == "absent"
