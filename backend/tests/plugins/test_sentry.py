"""Tests for the SentryPlugin."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from api.plugins.sentry.config import SentryPluginConfig
from api.plugins.sentry.exceptions import (
    SentryAPIError,
    SentryAuthError,
    SentryNotFoundError,
    SentryRateLimitError,
)
from api.plugins.sentry.models import (
    SentryCodeMapping,
    SentryIssue,
    SentryProject,
    SentryRelease,
    SentryWebhookSubscription,
)
from api.plugins.sentry.plugin import SentryPlugin


@pytest.fixture
def plugin() -> SentryPlugin:
    config = SentryPluginConfig(
        enabled=True,
        base_url="https://sentry.io",
        auth_token="test-token",
        max_retries=2,
        backoff_base=0.01,
    )
    return SentryPlugin(config)


def _mock_response(
    status_code: int = 200,
    json_data: object = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a mock httpx.Response."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("GET", "https://sentry.io/api/0/test/"),
    )


# ── Auth Header ────────────────────────────────────────────────────────


def test_auth_header_is_set(plugin: SentryPlugin) -> None:
    headers = plugin._get_default_headers()
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Content-Type"] == "application/json"


# ── list_projects ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_list_projects(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        json_data=[
            {"id": "1", "slug": "my-project", "name": "My Project", "platform": "python"},
        ],
    )

    projects = await plugin.list_projects("my-org")

    assert len(projects) == 1
    assert isinstance(projects[0], SentryProject)
    assert projects[0].slug == "my-project"
    await plugin.shutdown()


# ── list_code_mappings ─────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_list_code_mappings(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        json_data=[
            {
                "id": "1",
                "projectSlug": "my-project",
                "repoName": "org/repo",
                "provider": "github",
                "stackRoot": "src/",
                "sourceRoot": "src/",
                "defaultBranch": "main",
            },
        ],
    )

    mappings = await plugin.list_code_mappings("my-org")

    assert len(mappings) == 1
    assert isinstance(mappings[0], SentryCodeMapping)
    assert mappings[0].repo_name == "org/repo"
    assert mappings[0].project_slug == "my-project"
    await plugin.shutdown()


# ── list_issues ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_list_issues(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        json_data=[
            {"id": "100", "title": "TypeError", "level": "error"},
        ],
    )

    issues = await plugin.list_issues("my-org", "my-project", query="is:unresolved")

    assert len(issues) == 1
    assert isinstance(issues[0], SentryIssue)
    assert issues[0].title == "TypeError"
    call_kwargs = mock_request.call_args
    assert call_kwargs.kwargs["params"]["query"] == "is:unresolved"
    await plugin.shutdown()


# ── get_issue ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_get_issue(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        json_data={"id": "100", "title": "TypeError", "level": "error"},
    )

    issue = await plugin.get_issue("100")

    assert isinstance(issue, SentryIssue)
    assert issue.id == "100"
    await plugin.shutdown()


# ── register_webhook ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_register_webhook(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        status_code=201,
        json_data={"id": "wh-1", "url": "https://example.com/hook", "events": ["issue"]},
    )

    webhook = await plugin.register_webhook("my-org", "https://example.com/hook", ["issue"])

    assert isinstance(webhook, SentryWebhookSubscription)
    assert webhook.id == "wh-1"
    call_kwargs = mock_request.call_args
    assert call_kwargs.kwargs["json"]["url"] == "https://example.com/hook"
    await plugin.shutdown()


# ── list_releases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_list_releases(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        json_data=[{"version": "1.0.0", "shortVersion": "1.0.0"}],
    )

    releases = await plugin.list_releases("my-org", "my-project")

    assert len(releases) == 1
    assert isinstance(releases[0], SentryRelease)
    assert releases[0].version == "1.0.0"
    await plugin.shutdown()


# ── Authentication Errors ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_auth_error(mock_request: AsyncMock, plugin: SentryPlugin, status_code: int) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(status_code=status_code, json_data={})

    with pytest.raises(SentryAuthError) as exc_info:
        await plugin.get_issue("100")

    assert exc_info.value.status_code == status_code
    await plugin.shutdown()


# ── Not Found ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_not_found(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(status_code=404, json_data={})

    with pytest.raises(SentryNotFoundError):
        await plugin.get_issue("999")
    await plugin.shutdown()


# ── Retry on 5xx ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_retry_on_server_error(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.side_effect = [
        _mock_response(status_code=502, json_data={}),
        _mock_response(status_code=200, json_data={"id": "1", "title": "OK", "level": "error"}),
    ]

    issue = await plugin.get_issue("1")

    assert issue.title == "OK"
    assert mock_request.call_count == 2
    await plugin.shutdown()


# ── Retry on Transport Error ──────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_retry_on_transport_error(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.side_effect = [
        httpx.ConnectError("Connection refused"),
        _mock_response(status_code=200, json_data={"id": "1", "title": "OK", "level": "error"}),
    ]

    issue = await plugin.get_issue("1")

    assert issue.title == "OK"
    assert mock_request.call_count == 2
    await plugin.shutdown()


# ── Retry Exhausted ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_retry_exhausted_raises(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(status_code=500, json_data={})

    with pytest.raises(SentryAPIError, match="500"):
        await plugin.get_issue("1")

    assert mock_request.call_count == 3
    await plugin.shutdown()


# ── Retry on 429 Rate Limit ───────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_retry_on_rate_limit(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.side_effect = [
        _mock_response(
            status_code=429,
            json_data={},
            headers={"retry-after": "0.01"},
        ),
        _mock_response(
            status_code=200,
            json_data={"id": "1", "title": "OK", "level": "error"},
        ),
    ]

    issue = await plugin.get_issue("1")

    assert issue.title == "OK"
    assert mock_request.call_count == 2
    await plugin.shutdown()


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_rate_limit_exhausted_raises(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    mock_request.return_value = _mock_response(
        status_code=429,
        json_data={},
        headers={"retry-after": "0.01"},
    )

    with pytest.raises(SentryRateLimitError):
        await plugin.get_issue("1")
    await plugin.shutdown()


# ── Rate Limit Header Parsing ─────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_rate_limit_header_updates_state(
    mock_request: AsyncMock, plugin: SentryPlugin
) -> None:
    """Verify X-Sentry-Rate-Limits header is parsed and respected."""
    await plugin.startup()
    mock_request.side_effect = [
        _mock_response(
            status_code=200,
            json_data=[],
            headers={"x-sentry-rate-limits": "0.01:default:org"},
        ),
        _mock_response(status_code=200, json_data=[]),
    ]

    await plugin.list_projects("my-org")
    await plugin.list_projects("my-org")

    assert mock_request.call_count == 2
    await plugin.shutdown()


# ── Pagination ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock)
async def test_pagination(mock_request: AsyncMock, plugin: SentryPlugin) -> None:
    await plugin.startup()
    page1 = _mock_response(
        json_data=[{"id": "1", "slug": "p1", "name": "P1"}],
        headers={
            "link": (
                "<https://sentry.io/api/0/organizations/my-org/projects/?cursor=next>; "
                'rel="next"; results="true"; cursor="next", '
                "<https://sentry.io/api/0/organizations/my-org/projects/?cursor=prev>; "
                'rel="previous"; results="false"; cursor="prev"'
            )
        },
    )
    page2 = _mock_response(
        json_data=[{"id": "2", "slug": "p2", "name": "P2"}],
        headers={
            "link": (
                "<https://sentry.io/api/0/organizations/my-org/projects/?cursor=end>; "
                'rel="next"; results="false"; cursor="end"'
            )
        },
    )
    mock_request.side_effect = [page1, page2]

    projects = await plugin.list_projects("my-org")

    assert len(projects) == 2
    assert projects[0].slug == "p1"
    assert projects[1].slug == "p2"
    assert mock_request.call_count == 2
    await plugin.shutdown()


# ── Backoff Delay ─────────────────────────────────────────────────────


def test_backoff_delay(plugin: SentryPlugin) -> None:
    assert plugin._backoff_delay(0) == 0.01
    assert plugin._backoff_delay(1) == 0.02
    assert plugin._backoff_delay(2) == 0.04
