"""Unit tests for `apply_guardrail`."""

from __future__ import annotations

from unittest.mock import patch

from src.activities.review import apply_guardrail
from src.activities.review.schemas import GitHubComment, GitLabComment
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx() -> RunContext:
    from pathlib import Path

    return RunContext(cwd=Path("/tmp"), workspace=Path("/tmp"), events=EventBus())


def test_passes_clean_findings_through() -> None:
    comments = [
        GitHubComment(path="a.py", line=1, body="trivial finding"),
        GitHubComment(path="b.py", line=2, body="another finding"),
    ]
    with patch("src.activities.review.guardrail._scan", return_value=False):
        out = apply_guardrail(comments, ctx=_ctx())
    assert out == comments


def test_drops_flagged_finding() -> None:
    comments = [
        GitHubComment(path="a.py", line=1, body="ok"),
        GitHubComment(path="b.py", line=2, body="leaked-token-here"),
    ]
    with patch(
        "src.activities.review.guardrail._scan",
        side_effect=lambda body: body == "leaked-token-here",
    ):
        out = apply_guardrail(comments, ctx=_ctx())
    assert [c.body for c in out] == ["ok"]


def test_handles_gitlab_comments() -> None:
    comments = [
        GitLabComment(new_path="a.py", new_line=1, body="ok"),
    ]
    with patch("src.activities.review.guardrail._scan", return_value=False):
        out = apply_guardrail(comments, ctx=_ctx())
    assert out == comments


def test_handles_empty_list() -> None:
    out = apply_guardrail([], ctx=_ctx())
    assert out == []
