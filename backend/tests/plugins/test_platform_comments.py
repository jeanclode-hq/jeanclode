"""Tests for the marker-based comment/note upsert.

Verifies the find-by-marker → update, else create behavior that keeps
the sticky status comment to a single comment per target — the core
race-avoidance property ported from the predecessor project.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from api.plugins.container.platform_comments import upsert_github_comment, upsert_gitlab_note

MARKER = "<!-- jeanclode-status -->"


async def test_upsert_github_comment_creates_when_none_exists():
    plugin = AsyncMock()
    plugin.list_issue_comments.return_value = []

    await upsert_github_comment(
        plugin,
        installation_token="tok",
        owner="acme",
        repo="widgets",
        number=42,
        marker=MARKER,
        body=f"{MARKER}\nhello",
    )

    plugin.create_issue_comment.assert_awaited_once_with(
        "tok", "acme", "widgets", 42, f"{MARKER}\nhello"
    )
    plugin.update_issue_comment.assert_not_awaited()


async def test_upsert_github_comment_updates_existing_marked_comment():
    plugin = AsyncMock()
    plugin.list_issue_comments.return_value = [
        {"id": 1, "body": "unrelated comment"},
        {"id": 2, "body": f"{MARKER}\nold status"},
    ]

    await upsert_github_comment(
        plugin,
        installation_token="tok",
        owner="acme",
        repo="widgets",
        number=42,
        marker=MARKER,
        body=f"{MARKER}\nnew status",
    )

    plugin.update_issue_comment.assert_awaited_once_with(
        "tok", "acme", "widgets", 2, f"{MARKER}\nnew status"
    )
    plugin.create_issue_comment.assert_not_awaited()


async def test_upsert_gitlab_note_creates_when_none_exists():
    plugin = AsyncMock()
    plugin.list_notes.return_value = []

    await upsert_gitlab_note(
        plugin,
        access_token="tok",
        project_id="123",
        resource="merge_requests",
        iid=7,
        marker=MARKER,
        body=f"{MARKER}\nhello",
        provider_url="https://gitlab.example.com",
    )

    plugin.create_note.assert_awaited_once_with(
        "tok",
        "123",
        "merge_requests",
        7,
        f"{MARKER}\nhello",
        provider_url="https://gitlab.example.com",
    )
    plugin.update_note.assert_not_awaited()


async def test_upsert_gitlab_note_updates_existing_marked_note():
    plugin = AsyncMock()
    plugin.list_notes.return_value = [
        {"id": 5, "body": "unrelated note"},
        {"id": 9, "body": f"{MARKER}\nold status"},
    ]

    await upsert_gitlab_note(
        plugin,
        access_token="tok",
        project_id="123",
        resource="issues",
        iid=3,
        marker=MARKER,
        body=f"{MARKER}\nnew status",
        provider_url=None,
    )

    plugin.update_note.assert_awaited_once_with(
        "tok", "123", "issues", 3, 9, f"{MARKER}\nnew status", provider_url=None
    )
    plugin.create_note.assert_not_awaited()
