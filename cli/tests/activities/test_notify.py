"""``post_ready_notice`` — dedupe, gating, and per-provider comment shape."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from src.activities.notify import post_ready_notice
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.notify import READY_MARKER


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _completed(rc: int = 0, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _github_comments(*bodies: str) -> str:
    return json.dumps({"comments": [{"body": b} for b in bodies]})


def test_no_handles_is_a_no_op(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        assert post_ready_notice("github", "acme/app", "7", [], ctx=_ctx(tmp_path)) is False
    run.assert_not_called()


def test_missing_pr_is_a_no_op(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        assert post_ready_notice("github", "acme/app", "", ["alice"], ctx=_ctx(tmp_path)) is False
    run.assert_not_called()


def test_posts_a_github_comment_with_mentions(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [_completed(0, stdout=_github_comments("unrelated")), _completed(0)]
        assert post_ready_notice("github", "acme/app", "7", ["alice"], ctx=_ctx(tmp_path)) is True

    post = run.call_args_list[-1].args[0]
    assert post[:3] == ["gh", "pr", "comment"]
    body = post[post.index("--body") + 1]
    assert "@alice" in body
    assert READY_MARKER in body


def test_existing_marker_stops_a_second_notice(tmp_path: Path) -> None:
    """Both ends of the review loop can emit this, so whichever arrives
    second has to stand down."""
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [
            _completed(0, stdout=_github_comments(f"{READY_MARKER}\n@alice ready")),
        ]
        assert post_ready_notice("github", "acme/app", "7", ["alice"], ctx=_ctx(tmp_path)) is False

    assert run.call_count == 1  # looked, did not post


def test_unreadable_comment_list_still_posts(tmp_path: Path) -> None:
    """A missed ping is the bug this feature exists to fix; a duplicate is
    only noise. So a failed dedupe lookup errs towards posting."""
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [_completed(1, stderr="api down"), _completed(0)]
        assert post_ready_notice("github", "acme/app", "7", ["alice"], ctx=_ctx(tmp_path)) is True


def test_rejected_comment_reports_false_without_raising(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [_completed(0, stdout=_github_comments()), _completed(1, stderr="403")]
        assert post_ready_notice("github", "acme/app", "7", ["alice"], ctx=_ctx(tmp_path)) is False


def test_subprocess_failure_never_propagates(tmp_path: Path) -> None:
    """A failed notification must not fail the workflow that produced a
    perfectly good MR."""
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = OSError("gh missing")
        assert post_ready_notice("github", "acme/app", "7", ["alice"], ctx=_ctx(tmp_path)) is False


def test_gitlab_posts_a_note_against_the_mr_host(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [_completed(0, stdout="[]"), _completed(0)]
        posted = post_ready_notice(
            "gitlab",
            "group/app",
            "42",
            ["alice"],
            pr_url="https://gitlab.example.com/group/app/-/merge_requests/42",
            ctx=_ctx(tmp_path),
        )

    assert posted is True
    post = run.call_args_list[-1]
    assert post.args[0][:2] == ["glab", "api"]
    assert "projects/:id/merge_requests/42/notes" in post.args[0]
    assert post.kwargs["env"]["GITLAB_HOST"] == "https://gitlab.example.com"


def test_gitlab_marker_check_reads_notes(tmp_path: Path) -> None:
    with patch("src.activities.notify.subprocess.run") as run:
        run.side_effect = [_completed(0, stdout=json.dumps([{"body": READY_MARKER}]))]
        assert (
            post_ready_notice("gitlab", "group/app", "42", ["alice"], ctx=_ctx(tmp_path)) is False
        )
