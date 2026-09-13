"""JEANCLODE_NOTIFY_USERS parsing + the ready-notice body."""

from __future__ import annotations

import json

from src.runtime.bots import login_is_bot
from src.runtime.notify import (
    READY_MARKER,
    load_notify_users_from_env,
    ready_notice_body,
)


def test_no_env_var_is_the_off_switch() -> None:
    assert load_notify_users_from_env({}) == []
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": "  "}) == []


def test_parses_handles_preserving_order() -> None:
    payload = json.dumps(["alice", "bob", "carol"])
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": payload}) == [
        "alice",
        "bob",
        "carol",
    ]


def test_malformed_json_yields_no_handles() -> None:
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": "not json"}) == []


def test_non_array_payload_yields_no_handles() -> None:
    payload = json.dumps({"alice": True})
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": payload}) == []


def test_duplicates_collapse() -> None:
    payload = json.dumps(["alice", "alice", "bob"])
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": payload}) == ["alice", "bob"]


def test_leading_at_is_stripped() -> None:
    payload = json.dumps(["@alice"])
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": payload}) == ["alice"]


def test_handles_that_would_become_markup_are_dropped() -> None:
    """These end up as live text in someone's repo, so anything that isn't a
    bare handle is dropped rather than rendered."""
    payload = json.dumps(
        [
            "alice",
            "bob and friends",  # whitespace
            "[link](http://evil)",  # markdown
            "",  # empty
            None,  # not a string
            "x" * 100,  # absurd length
            "carol.d-e_f",  # legitimately punctuated
        ]
    )
    assert load_notify_users_from_env({"JEANCLODE_NOTIFY_USERS": payload}) == [
        "alice",
        "carol.d-e_f",
    ]


def test_body_carries_the_marker_and_mentions() -> None:
    body = ready_notice_body(["alice", "bob"])
    assert body.startswith(READY_MARKER)
    assert "@alice @bob" in body


def test_clean_review_and_findings_read_differently() -> None:
    """A ping on an MR with open findings must not imply the bot signed off."""
    clean = ready_notice_body(["alice"])
    with_findings = ready_notice_body(["alice"], findings=3)

    assert "clean" in clean
    assert "3 review comments" in with_findings
    assert "clean" not in with_findings


def test_single_finding_is_singular() -> None:
    assert "1 review comment " in ready_notice_body(["alice"], findings=1)


def test_login_is_bot_matches_provider_token_shapes() -> None:
    assert login_is_bot("jeanclode-bot[bot]", "github")
    assert login_is_bot("group_42_bot_abc123", "gitlab")
    assert login_is_bot("project_7_bot", "gitlab")
    assert login_is_bot("jeanclode-bot", "gitlab")
    assert not login_is_bot("alice", "github")
    assert not login_is_bot("", "github")
    # A bare `-bot` suffix is a GitLab token shape, not a GitHub one:
    # GitHub bots always carry the `[bot]` suffix.
    assert not login_is_bot("jeanclode-bot", "github")
