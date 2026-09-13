"""Unit tests for `style_comments`."""

from __future__ import annotations

from pathlib import Path

from src.activities.review import style_comments
from src.activities.review.schemas import GitHubComment, GitLabComment
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx() -> RunContext:
    return RunContext(cwd=Path("/tmp"), workspace=Path("/tmp"), events=EventBus())


def test_applies_bodies_in_order() -> None:
    comments = [
        GitHubComment(path="a.py", line=1, body="orig0"),
        GitHubComment(path="b.py", line=2, body="orig1"),
    ]
    out = style_comments(comments, ["new0", "new1"], ctx=_ctx())
    assert [c.body for c in out] == ["new0", "new1"]
    assert [c.path for c in out] == ["a.py", "b.py"]


def test_falls_back_on_length_mismatch() -> None:
    comments = [
        GitHubComment(path="a.py", line=1, body="orig0"),
        GitHubComment(path="b.py", line=2, body="orig1"),
    ]
    out = style_comments(comments, ["only one"], ctx=_ctx())
    assert [c.body for c in out] == ["orig0", "orig1"]


def test_keeps_original_when_body_blank() -> None:
    comments = [GitHubComment(path="a.py", line=1, body="orig")]
    out = style_comments(comments, ["   "], ctx=_ctx())
    assert out[0].body == "orig"


def test_none_bodies_keeps_originals() -> None:
    comments = [GitHubComment(path="a.py", line=1, body="orig")]
    out = style_comments(comments, None, ctx=_ctx())
    assert out[0].body == "orig"


def test_handles_gitlab_comments() -> None:
    comments = [GitLabComment(new_path="a.py", new_line=1, body="orig")]
    out = style_comments(comments, ["new"], ctx=_ctx())
    assert out[0].body == "new"
    assert out[0].new_path == "a.py"
