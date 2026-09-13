"""ci-watch gating state machine — same function backs the agent tool and
the runner's own authoritative post-session check."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.activities.ci_watch.poll import check_ci
from src.activities.ci_watch.schemas import CheckResult
from src.activities.git.schemas import PRRef


def _pr(platform: str = "github") -> PRRef:
    url = (
        "https://github.com/org/repo/pull/9"
        if platform == "github"
        else "https://gitlab.com/g/p/-/merge_requests/9"
    )
    return PRRef(url=url, branch="fix/x", platform=platform)  # type: ignore[arg-type]


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
def test_no_ci_finishes_after_settle_window(
    _sleep: Any,
    _head: Any,
    discover_head: Any,
    _weighting: Any,
    tmp_path: Path,
) -> None:
    discover_head.return_value = []
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "finish"
    assert "no CI configured" in result.reason


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
def test_all_success_finishes(
    _sleep: Any, _head: Any, discover_head: Any, _weighting: Any, tmp_path: Path
) -> None:
    discover_head.return_value = [CheckResult(name="build", bucket="success")]
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "finish"


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
def test_blocked_only_finishes(
    _sleep: Any, _head: Any, discover_head: Any, _weighting: Any, tmp_path: Path
) -> None:
    discover_head.return_value = [
        CheckResult(name="deploy", bucket="blocked", raw_conclusion="action_required")
    ]
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "finish"


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
def test_required_failure_fails(
    _sleep: Any, _head: Any, discover_head: Any, _weighting: Any, tmp_path: Path
) -> None:
    discover_head.return_value = [CheckResult(name="test", bucket="failure", required=True)]
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "failure"
    assert "test" in result.reason


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
def test_non_required_failure_still_finishes(
    _sleep: Any, _head: Any, discover_head: Any, _weighting: Any, tmp_path: Path
) -> None:
    # required-checks weighting: a failing but non-required (e.g. advisory
    # preview-deploy) check must not block gating.
    discover_head.return_value = [
        CheckResult(name="preview-deploy", bucket="failure", required=False)
    ]
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "finish"


@patch("src.activities.ci_watch.poll._apply_required_weighting")
@patch("src.activities.ci_watch.poll._discover_head")
@patch("src.activities.ci_watch.poll._head_sha", return_value="sha1")
@patch("src.activities.ci_watch.poll.time.sleep")
@patch("src.activities.ci_watch.poll._WAIT_TIMEOUT_SECONDS", 0.0)
def test_own_wait_timeout_counts_as_failure(
    _sleep: Any, _head: Any, discover_head: Any, _weighting: Any, tmp_path: Path
) -> None:
    discover_head.return_value = [CheckResult(name="test", bucket="running")]
    result = check_ci(_pr(), cwd=tmp_path)
    assert result.outcome == "failure"
    assert "timed out" in result.reason
