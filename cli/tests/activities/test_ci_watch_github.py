"""ci-watch GitHub discovery — Checks API + legacy Status API, via `gh`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.activities.ci_watch import github
from src.activities.ci_watch.schemas import LOG_EXCERPT_CHARS


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_success_check_run(run: Any, tmp_path: Path) -> None:
    run.side_effect = [
        _completed(
            json.dumps(
                {"check_runs": [{"name": "build", "status": "completed", "conclusion": "success"}]}
            )
        ),
        _completed(json.dumps({"statuses": []})),
    ]
    checks = github.discover("abc123", cwd=tmp_path, fetch_logs=False)
    assert len(checks) == 1
    assert checks[0].name == "build"
    assert checks[0].bucket == "success"


@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_running_check_run(run: Any, tmp_path: Path) -> None:
    run.side_effect = [
        _completed(
            json.dumps(
                {"check_runs": [{"name": "build", "status": "in_progress", "conclusion": None}]}
            )
        ),
        _completed(json.dumps({"statuses": []})),
    ]
    checks = github.discover("abc123", cwd=tmp_path, fetch_logs=False)
    assert checks[0].bucket == "running"


@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_blocked_action_required(run: Any, tmp_path: Path) -> None:
    run.side_effect = [
        _completed(
            json.dumps(
                {
                    "check_runs": [
                        {"name": "deploy", "status": "completed", "conclusion": "action_required"}
                    ]
                }
            )
        ),
        _completed(json.dumps({"statuses": []})),
    ]
    checks = github.discover("abc123", cwd=tmp_path, fetch_logs=False)
    assert checks[0].bucket == "blocked"


@patch("src.activities.ci_watch.github.failed_run_log")
@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_failure_fetches_log(run: Any, log: Any, tmp_path: Path) -> None:
    details_url = "https://github.com/org/repo/actions/runs/1/job/2"
    run.side_effect = [
        _completed(
            json.dumps(
                {
                    "check_runs": [
                        {
                            "name": "test",
                            "status": "completed",
                            "conclusion": "failure",
                            "details_url": details_url,
                        }
                    ]
                }
            )
        ),
        _completed(json.dumps({"statuses": []})),
    ]
    log.return_value = "boom"
    checks = github.discover("abc123", cwd=tmp_path, fetch_logs=True)
    assert checks[0].bucket == "failure"
    assert checks[0].log_excerpt == "boom"
    log.assert_called_once_with(details_url, cwd=tmp_path)


@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_merges_legacy_status_api(run: Any, tmp_path: Path) -> None:
    run.side_effect = [
        _completed(json.dumps({"check_runs": []})),
        _completed(json.dumps({"statuses": [{"context": "circleci", "state": "success"}]})),
    ]
    checks = github.discover("abc123", cwd=tmp_path, fetch_logs=False)
    assert len(checks) == 1
    assert checks[0].name == "circleci"
    assert checks[0].bucket == "success"


@patch("src.activities.ci_watch.github.subprocess.run")
def test_discover_returns_empty_on_no_ci(run: Any, tmp_path: Path) -> None:
    run.side_effect = [_completed(returncode=1), _completed(returncode=1)]
    assert github.discover("abc123", cwd=tmp_path, fetch_logs=False) == []


@patch("src.activities.ci_watch.github.subprocess.run")
def test_required_check_names_best_effort_none_on_403(run: Any, tmp_path: Path) -> None:
    run.return_value = _completed(returncode=1)
    assert github.required_check_names("main", cwd=tmp_path) is None


@patch("src.activities.ci_watch.github.subprocess.run")
def test_required_check_names_parses_contexts(run: Any, tmp_path: Path) -> None:
    run.return_value = _completed(json.dumps({"contexts": ["build", "test"]}))
    assert github.required_check_names("main", cwd=tmp_path) == {"build", "test"}


@patch("src.activities.ci_watch.github.subprocess.run")
def test_failed_run_log_parses_run_and_job_id_from_details_url(run: Any, tmp_path: Path) -> None:
    # Real shape, verified against a live check-run: check-run names are
    # per-job ("test-backend") while `gh run list` only exposes per-workflow
    # names ("CI") — they never match, so the run/job id is parsed directly
    # out of details_url instead of matching by name.
    run.return_value = _completed("failure output here")
    log = github.failed_run_log(
        "https://github.com/org/repo/actions/runs/30306492098/job/90111902200", cwd=tmp_path
    )
    assert log == "failure output here"
    assert run.call_args[0][0] == [
        "gh",
        "run",
        "view",
        "30306492098",
        "--job",
        "90111902200",
        "--log-failed",
    ]


@patch("src.activities.ci_watch.github.subprocess.run")
def test_failed_run_log_truncates_from_the_end(run: Any, tmp_path: Path) -> None:
    huge = "x" * 5000 + "END"
    run.return_value = _completed(huge)
    log = github.failed_run_log("https://github.com/org/repo/actions/runs/1/job/2", cwd=tmp_path)
    assert log.endswith("END")
    assert len(log) == LOG_EXCERPT_CHARS


@patch("src.activities.ci_watch.github.subprocess.run")
def test_failed_run_log_returns_empty_for_non_actions_details_url(run: Any, tmp_path: Path) -> None:
    # e.g. a Cloudflare/CodeQL/third-party check-run's details_url, which
    # doesn't have the .../actions/runs/{id}/job/{id} shape.
    log = github.failed_run_log("https://dash.cloudflare.com/some/build/id", cwd=tmp_path)
    assert log == ""
    run.assert_not_called()
