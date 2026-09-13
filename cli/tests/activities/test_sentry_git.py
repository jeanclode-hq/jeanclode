"""create_worktree / push_branch — git subprocess invocations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.activities.sentry import (
    WorktreePath,
    create_worktree,
    fix_missing_reason,
    push_branch,
    verify_fix_pushed,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path / "repo", workspace=tmp_path, events=EventBus())


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@patch("src.activities.git.ops.subprocess.run")
def test_create_worktree_returns_path_and_branch(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("refs/remotes/origin/main\n"),  # symbolic-ref
        _completed(),  # worktree add
        _completed(),  # empty commit
        _completed("placeholder-sha\n"),  # rev-parse HEAD
    ]
    wt = create_worktree("fix/sentry-abc-1", ctx=ctx)
    assert isinstance(wt, WorktreePath)
    assert wt.branch == "fix/sentry-abc-1"
    assert wt.path == ctx.workspace / "worktrees" / "fix_sentry-abc-1"
    assert wt.placeholder_sha == "placeholder-sha"

    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]
    assert cmds[1][:3] == ["git", "worktree", "add"]
    assert "-b" in cmds[1]
    assert "fix/sentry-abc-1" in cmds[1]
    assert cmds[2][:2] == ["git", "commit"]
    assert "--allow-empty" in cmds[2]
    assert cmds[3] == ["git", "rev-parse", "HEAD"]


@patch("src.activities.git.ops.subprocess.run")
def test_create_worktree_falls_back_to_main_when_head_missing(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        subprocess.CalledProcessError(returncode=1, cmd=["git"], stderr=b""),
        _completed(),
        _completed(),
        _completed("placeholder-sha\n"),
    ]
    create_worktree("fix/x", ctx=ctx)
    add_cmd = run.call_args_list[1].args[0]
    assert add_cmd[-1] == "main"


@patch("src.activities.git.ops.subprocess.run")
def test_push_branch_uses_upstream(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed()
    push_branch("fix/x", ctx=ctx)
    assert run.call_args.args[0] == ["git", "push", "-f", "-u", "origin", "fix/x"]
    assert run.call_args.kwargs["cwd"] == ctx.cwd


@patch("src.activities.git.ops.subprocess.run")
def test_push_branch_propagates_failure(run: Any, ctx: RunContext) -> None:
    run.side_effect = subprocess.CalledProcessError(1, ["git"])
    with pytest.raises(subprocess.CalledProcessError):
        push_branch("fix/x", ctx=ctx)


# ── fix_missing_reason / verify_fix_pushed ─────────────────────────────────


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_flags_head_still_at_placeholder(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("placeholder-sha\n"),  # rev-parse HEAD: unchanged
    ]
    reason = fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha")
    assert reason is not None
    assert "no code changes" in reason.lower()


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_flags_unpushed_commit(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("real-fix-sha\n"),  # rev-parse HEAD: moved past placeholder
        _completed("stale-remote-sha\trefs/heads/fix/x\n"),  # ls-remote: behind HEAD
    ]
    reason = fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha")
    assert reason is not None
    assert "haven't been pushed" in reason.lower()


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_flags_branch_missing_on_remote(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("real-fix-sha\n"),  # rev-parse HEAD
        _completed(""),  # ls-remote: branch doesn't exist on origin at all
    ]
    reason = fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha")
    assert reason is not None
    assert "haven't been pushed" in reason.lower()


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_none_when_pushed(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("real-fix-sha\n"),  # rev-parse HEAD
        _completed("real-fix-sha\trefs/heads/fix/x\n"),  # ls-remote: matches HEAD
    ]
    assert fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha") is None


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_ignores_dirty_working_tree_when_pushed(
    run: Any, ctx: RunContext
) -> None:
    """A dirty working tree (e.g. bytecode cache from the fixer's own
    static-check step) must not block gating — a pushed commit is the
    signal that matters, not local cleanliness afterward."""
    run.side_effect = [
        _completed("real-fix-sha\n"),  # rev-parse HEAD
        _completed("real-fix-sha\trefs/heads/fix/x\n"),  # ls-remote: matches HEAD
    ]
    assert fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha") is None
    commands = [c.args[0] for c in run.call_args_list]
    assert ["git", "status", "--porcelain"] not in commands


@patch("src.activities.git.ops.subprocess.run")
def test_verify_fix_pushed_delegates_to_fix_missing_reason(run: Any, ctx: RunContext) -> None:
    run.side_effect = [_completed("placeholder-sha\n")]
    reason = verify_fix_pushed("fix/x", "placeholder-sha", ctx=ctx)
    assert reason is not None


@patch("src.activities.git.ops.subprocess.run")
def test_fix_missing_reason_raises_when_ls_remote_fails(run: Any, ctx: RunContext) -> None:
    run.side_effect = [
        _completed("real-fix-sha\n"),  # rev-parse HEAD
        _completed(returncode=1),  # git ls-remote origin branch: fails
    ]
    with pytest.raises(RuntimeError, match="Could not verify"):
        fix_missing_reason(ctx.cwd, "fix/x", "placeholder-sha")
