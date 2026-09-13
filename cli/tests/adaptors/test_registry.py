"""Tests for the adaptor registry and detection."""

from __future__ import annotations

import pytest

from src.adaptors import find_adaptor, list_commands
from src.runner.preflight import detect_adaptor


def test_list_commands_unions_skills() -> None:
    cmds = list_commands()
    assert "sentry" in cmds
    assert "review" in cmds
    assert "summary" in cmds


def test_find_adaptor_sentry() -> None:
    a = find_adaptor("https://sentry.io/issues/1")
    assert a is not None
    assert a.name == "sentry"


def test_find_adaptor_sentry_self_hosted() -> None:
    a = find_adaptor("https://sentry.example.com/issues/1883122/")
    assert a is not None
    assert a.name == "sentry"


def test_find_adaptor_github() -> None:
    a = find_adaptor("https://github.com/org/repo/pull/42")
    assert a is not None
    assert a.name == "github"


def test_find_adaptor_gitlab() -> None:
    a = find_adaptor("https://gitlab.com/g/p/-/merge_requests/1")
    assert a is not None
    assert a.name == "gitlab"


def test_find_adaptor_unknown() -> None:
    assert find_adaptor("https://example.com/foo") is None


def test_detect_adaptor_default_command() -> None:
    adaptor, skill = detect_adaptor("https://github.com/org/repo/pull/1")
    assert adaptor.name == "github"
    assert skill == "code-review"


def test_detect_adaptor_explicit_review() -> None:
    _adaptor, skill = detect_adaptor("https://github.com/org/repo/pull/1", command="review")
    assert skill == "code-review"


def test_detect_adaptor_summary() -> None:
    _adaptor, skill = detect_adaptor("https://github.com/org/repo/pull/1", command="summary")
    assert skill == "pr-summary"


def test_detect_adaptor_gitlab_summary() -> None:
    adaptor, skill = detect_adaptor("https://gitlab.com/g/p/-/merge_requests/1", command="summary")
    assert adaptor.name == "gitlab"
    assert skill == "pr-summary"


def test_detect_adaptor_unknown_url_raises() -> None:
    with pytest.raises(ValueError, match="Could not detect"):
        detect_adaptor("https://example.com/foo")


def test_detect_adaptor_unsupported_command_raises() -> None:
    with pytest.raises(ValueError, match="does not support"):
        detect_adaptor("https://sentry.io/issues/1", command="review")


def test_detect_adaptor_sentry_default() -> None:
    adaptor, skill = detect_adaptor("https://sentry.io/issues/1")
    assert adaptor.name == "sentry"
    assert skill == "sentry-fix"


def test_detect_adaptor_sentry_self_hosted() -> None:
    adaptor, skill = detect_adaptor("https://sentry.example.com/issues/1883122/")
    assert adaptor.name == "sentry"
    assert skill == "sentry-fix"
