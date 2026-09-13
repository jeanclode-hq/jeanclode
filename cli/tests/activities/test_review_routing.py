"""Unit tests for `filter_and_route`."""

from __future__ import annotations

from pathlib import Path

from src.activities.review import filter_and_route
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _write_diff(tmp_path: Path, body: str) -> None:
    cache = tmp_path / ".context"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "diff").write_text(body)


def test_stops_on_empty_diff(tmp_path: Path) -> None:
    _write_diff(tmp_path, "")
    decision = filter_and_route(ctx=_ctx(tmp_path))
    assert decision.action == "stop"
    assert "empty" in decision.reason


def test_stops_on_oversized_diff(tmp_path: Path) -> None:
    _write_diff(tmp_path, "x" * 1_000_000)
    decision = filter_and_route(ctx=_ctx(tmp_path))
    assert decision.action == "stop"
    assert "too large" in decision.reason


def test_stops_on_lockfile_only(tmp_path: Path) -> None:
    _write_diff(tmp_path, "diff --git a/package-lock.json b/package-lock.json\n+ {}\n")
    decision = filter_and_route(ctx=_ctx(tmp_path))
    assert decision.action == "stop"
    assert "lockfile" in decision.reason


def test_reviews_real_diff(tmp_path: Path) -> None:
    _write_diff(tmp_path, "diff --git a/src/main.py b/src/main.py\n+def f(): pass\n")
    decision = filter_and_route(ctx=_ctx(tmp_path))
    assert decision.action == "review"


def test_missing_cache_returns_stop(tmp_path: Path) -> None:
    decision = filter_and_route(ctx=_ctx(tmp_path))
    assert decision.action == "stop"
    assert "missing" in decision.reason
