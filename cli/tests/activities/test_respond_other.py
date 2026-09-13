"""Unit tests for the ``relabel_pr`` activity."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from src.activities.respond import MentionContext, relabel_pr
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx(tmp_path: Path):
    received: list = []
    bus = EventBus()
    bus.subscribe(received.append)
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=bus), received


def _mention(**overrides: object) -> MentionContext:
    base = {"platform": "github", "repo": "acme/app", "pr": "7", "surface": "pr_top_level"}
    base.update(overrides)
    return MentionContext.model_validate(base)


def _completed(rc: int = 0, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_relabel_pr_does_remove_then_add(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    with patch("src.activities.respond.relabel.subprocess.run") as run:
        run.return_value = _completed(0)
        result = relabel_pr("jeanclode:review", _mention(), ctx=ctx)

    assert result.relabeled is True
    calls = [call.args[0] for call in run.call_args_list]
    assert any("--remove-label" in c for c in calls)
    assert any("--add-label" in c for c in calls)


def test_relabel_pr_missing_pr_returns_error(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    result = relabel_pr("jeanclode:review", _mention(pr=""), ctx=ctx)
    assert result.relabeled is False
    assert "missing pr" in result.error


def test_relabel_pr_gitlab_uses_glab(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    with patch("src.activities.respond.relabel.subprocess.run") as run:
        run.return_value = _completed(0)
        result = relabel_pr(
            "jeanclode:review",
            _mention(
                platform="gitlab", target_url="https://gitlab.com/acme/app/-/merge_requests/7"
            ),
            ctx=ctx,
        )

    assert result.relabeled is True
    calls = [call.args[0] for call in run.call_args_list]
    assert any(c[:2] == ["glab", "mr"] for c in calls)
