"""Unit tests for `dedup_against_existing_comments`."""

from __future__ import annotations

from pathlib import Path

from src.activities.review import dedup_against_existing_comments
from src.activities.review.schemas import GitHubComment
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx() -> RunContext:
    return RunContext(cwd=Path("/tmp"), workspace=Path("/tmp"), events=EventBus())


def _comments(n: int) -> list[GitHubComment]:
    return [GitHubComment(path=f"f{i}.py", line=i + 1, body=f"b{i}") for i in range(n)]


def test_keeps_indices_in_order() -> None:
    out = dedup_against_existing_comments(_comments(4), [0, 2], ctx=_ctx())
    assert [c.path for c in out] == ["f0.py", "f2.py"]


def test_filters_out_of_range_indices() -> None:
    out = dedup_against_existing_comments(_comments(2), [0, 5, -1], ctx=_ctx())
    assert [c.path for c in out] == ["f0.py"]


def test_none_keeps_all() -> None:
    out = dedup_against_existing_comments(_comments(3), None, ctx=_ctx())
    assert len(out) == 3


def test_empty_indices_keeps_none() -> None:
    out = dedup_against_existing_comments(_comments(3), [], ctx=_ctx())
    assert out == []


def test_dedup_indices() -> None:
    out = dedup_against_existing_comments(_comments(3), [1, 1, 0], ctx=_ctx())
    assert [c.path for c in out] == ["f0.py", "f1.py"]
