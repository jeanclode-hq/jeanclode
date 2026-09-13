"""fetch_sentry_data — turn issue URLs into formatted SentryEvents."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.activities.sentry import SentryEvent, fetch_sentry_data
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        env={"SENTRY_AUTH_TOKEN": "tok", "SENTRY_API_URL": "https://us.sentry.io"},
    )


_ISSUE_PAYLOAD = {
    "id": "12345",
    "title": "AttributeError",
    "status": "unresolved",
    "level": "error",
    "count": 5,
    "culprit": "user.py",
    "permalink": "https://sentry.io/issues/12345",
    "project": {"slug": "api"},
    "metadata": {"value": "NoneType has no attr profile"},
}

_EVENT_PAYLOAD = {
    "eventID": "abc",
    "title": "AttributeError",
    "release": {"version": "deadbee"},
    "tags": [{"key": "env", "value": "prod"}],
    "entries": [
        {
            "type": "exception",
            "data": {
                "values": [
                    {
                        "type": "AttributeError",
                        "value": "NoneType has no attribute profile",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "src/user.py",
                                    "lineno": 42,
                                    "function": "get_profile",
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            },
        }
    ],
}


def _api_response(body: dict | list) -> Any:
    """Return a fake _api_get callable bound to a payload."""
    return body


@patch("src.activities.sentry.fetch._api_get")
@patch("src.activities.sentry.fetch._resolve_api_url_for", return_value="https://us.sentry.io")
def test_fetch_one_url_yields_formatted_event(_resolve: Any, api_get: Any, ctx: RunContext) -> None:
    api_get.side_effect = [_ISSUE_PAYLOAD, _EVENT_PAYLOAD, []]  # issue, event, mappings
    events = fetch_sentry_data(["https://acme.sentry.io/issues/12345/"], ctx=ctx)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, SentryEvent)
    assert event.issue_id == "12345"
    assert event.commit_sha == "deadbee"
    assert "AttributeError" in event.formatted
    assert "src/user.py:42" in event.formatted


@patch("src.activities.sentry.fetch._api_get")
@patch("src.activities.sentry.fetch._resolve_api_url_for", return_value="https://us.sentry.io")
def test_fetch_skips_invalid_url(_resolve: Any, _api_get: Any, ctx: RunContext) -> None:
    events = fetch_sentry_data(["not-a-sentry-url"], ctx=ctx)
    assert events == []


@patch("src.activities.sentry.fetch._api_get")
@patch("src.activities.sentry.fetch._resolve_api_url_for", return_value="https://us.sentry.io")
def test_fetch_returns_empty_on_api_error(_resolve: Any, api_get: Any, ctx: RunContext) -> None:
    api_get.side_effect = RuntimeError("boom")
    events = fetch_sentry_data(["https://acme.sentry.io/issues/1"], ctx=ctx)
    assert events == []


@patch("src.activities.sentry.fetch._api_get")
@patch("src.activities.sentry.fetch._resolve_api_url_for", return_value="https://us.sentry.io")
def test_fetch_attaches_repo_url_from_code_mappings(
    _resolve: Any, api_get: Any, ctx: RunContext
) -> None:
    mappings = [{"projectSlug": "api", "repoName": "org/repo"}]
    api_get.side_effect = [_ISSUE_PAYLOAD, _EVENT_PAYLOAD, mappings]
    events = fetch_sentry_data(["https://acme.sentry.io/issues/12345"], ctx=ctx)
    assert events[0].repo_url == "https://github.com/org/repo"
