"""open_pr / attach_label — subprocess invocations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.activities.git import close_pr
from src.activities.sentry import PRRef, attach_label, open_pr
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_github(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed("https://github.com/org/repo/pull/9\n")
    pr = open_pr("fix/branch", "fix: x", "body", "github", ctx=ctx)
    assert isinstance(pr, PRRef)
    assert pr.url == "https://github.com/org/repo/pull/9"
    assert pr.platform == "github"
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "create"]
    assert "fix/branch" in cmd
    assert "--draft" not in cmd


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_gitlab(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed("https://gitlab.com/g/p/-/merge_requests/3\n")
    pr = open_pr("fix/b", "fix: y", "body", "gitlab", ctx=ctx)
    assert pr.platform == "gitlab"
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["glab", "mr", "create"]
    # A "Draft:" title makes GitLab skip the MR pipeline under webshop-style
    # `workflow: rules`, and un-drafting never triggers one — the MR would be
    # stuck on ci_must_pass forever.
    assert "--draft" not in cmd


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_gitlab_pins_the_source_branch(run: Any, ctx: RunContext) -> None:
    """`glab mr create` otherwise infers the source from the cwd's checked-out
    branch, so a cwd that isn't the worktree for this branch opens an MR from
    the wrong one — silently, and as a second MR."""
    run.return_value = _completed("https://gitlab.com/g/p/-/merge_requests/3\n")
    open_pr("fix/sentry-abc1234", "fix: y", "body", "gitlab", ctx=ctx)
    assert "--source-branch=fix/sentry-abc1234" in run.call_args[0][0]


@patch("src.activities.git.pr.subprocess.run")
def test_close_pr_github(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed()
    close_pr(
        PRRef(url="https://github.com/org/repo/pull/9", branch="b", platform="github"), ctx=ctx
    )
    assert run.call_args[0][0] == ["gh", "pr", "close", "9"]


@patch("src.activities.git.pr.subprocess.run")
def test_close_pr_gitlab_comments_first(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed()
    close_pr(
        PRRef(url="https://gitlab.com/g/p/-/merge_requests/3", branch="b", platform="gitlab"),
        ctx=ctx,
        comment="no change needed",
    )
    cmds = [c[0][0] for c in run.call_args_list]
    assert cmds[0] == ["glab", "mr", "note", "3", "--message", "no change needed"]
    assert cmds[-1] == ["glab", "mr", "close", "3"]


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_reuses_single_open_match_gitlab(run: Any, ctx: RunContext) -> None:
    run.return_value = _completed(
        '[{"web_url": "https://gitlab.com/g/p/-/merge_requests/634", "state": "opened"}]'
    )
    pr = open_pr("fix/b", "fix: y", "body", "gitlab", ctx=ctx)
    assert pr.url == "https://gitlab.com/g/p/-/merge_requests/634"
    # only the lookup ran — "mr create" was never reached
    assert run.call_count == 1
    assert run.call_args[0][0][:3] == ["glab", "mr", "list"]


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_creates_when_branch_has_only_closed_history_gitlab(
    run: Any, ctx: RunContext
) -> None:
    # --source-branch defaults to open-only, so closed MRs from earlier
    # attempts on this deterministic branch surface as an empty match list —
    # this must fall through to creating a fresh MR, not error or reuse one.
    run.side_effect = [
        _completed("[]"),
        _completed("https://gitlab.com/g/p/-/merge_requests/636\n"),
    ]
    pr = open_pr("fix/b", "fix: y", "body", "gitlab", ctx=ctx)
    assert pr.url == "https://gitlab.com/g/p/-/merge_requests/636"
    assert run.call_count == 2
    assert run.call_args_list[1][0][0][:3] == ["glab", "mr", "create"]


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (
            "github",
            "https://github.com/org/repo/pull/9",
            ["gh", "pr", "edit", "9", "--add-label", "jeanclode:review"],
        ),
        (
            "gitlab",
            "https://gitlab.com/g/p/-/merge_requests/636",
            ["glab", "mr", "update", "636", "--label", "jeanclode:review"],
        ),
    ],
)
@patch("src.activities.git.pr.subprocess.run")
def test_attach_label(
    run: Any, ctx: RunContext, platform: str, url: str, expected: list[str]
) -> None:
    run.return_value = _completed()
    pr = PRRef(url=url, branch="b", platform=platform)  # type: ignore[arg-type]
    attach_label(pr, "jeanclode:review", ctx=ctx)
    assert run.call_args[0][0] == expected


@pytest.mark.parametrize(
    ("platform", "url", "expected_remove", "expected_add"),
    [
        (
            "github",
            "https://github.com/org/repo/pull/9",
            ["gh", "pr", "edit", "9", "--remove-label", "jeanclode:review"],
            ["gh", "pr", "edit", "9", "--add-label", "jeanclode:review"],
        ),
        (
            "gitlab",
            "https://gitlab.com/g/p/-/merge_requests/636",
            ["glab", "mr", "update", "636", "--unlabel", "jeanclode:review"],
            ["glab", "mr", "update", "636", "--label", "jeanclode:review"],
        ),
    ],
)
@patch("src.activities.git.pr.subprocess.run")
def test_attach_label_removes_before_adding(
    run: Any,
    ctx: RunContext,
    platform: str,
    url: str,
    expected_remove: list[str],
    expected_add: list[str],
) -> None:
    # gh/glab's add-label is a no-op when the label is already present — no
    # webhook fires. A retried run re-attaching the same label needs a fresh
    # `labeled` event to re-trigger review/summary, so this must always
    # remove first (see docs/architecture/autonomous-loop.md).
    run.return_value = _completed()
    pr = PRRef(url=url, branch="b", platform=platform)  # type: ignore[arg-type]
    attach_label(pr, "jeanclode:review", ctx=ctx)
    calls = [c.args[0] for c in run.call_args_list]
    assert calls == [expected_remove, expected_add]


@patch("src.activities.git.pr.subprocess.run")
def test_attach_label_add_failure_raises_even_if_remove_failed(run: Any, ctx: RunContext) -> None:
    # The remove is best-effort (e.g. label wasn't there to begin with) —
    # only a failing add should surface as an error.
    run.side_effect = [
        _completed(returncode=1),  # remove: fails, ignored
        _completed(returncode=1, stdout=""),  # add: fails, must raise
    ]
    pr = PRRef(url="https://github.com/org/repo/pull/9", branch="b", platform="github")
    with pytest.raises(RuntimeError):
        attach_label(pr, "jeanclode:review", ctx=ctx)


@patch("src.activities.git.pr.subprocess.run")
def test_open_pr_propagates_subprocess_error(run: Any, ctx: RunContext) -> None:
    run.side_effect = subprocess.CalledProcessError(returncode=1, cmd=["gh"], stderr="boom")
    with pytest.raises(subprocess.CalledProcessError):
        open_pr("b", "t", "body", "github", ctx=ctx)
