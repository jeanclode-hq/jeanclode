"""ci-watch GitLab discovery — MR pipeline status is authoritative, via `glab`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.activities.ci_watch import gitlab


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@patch("src.activities.ci_watch.gitlab.subprocess.run")
def test_discover_head_matches_by_sha(run: Any, tmp_path: Path) -> None:
    run.return_value = _completed(
        json.dumps(
            [
                {"id": 1, "sha": "old", "status": "success"},
                {
                    "id": 2,
                    "sha": "new",
                    "status": "success",
                    "web_url": "https://gitlab.com/g/p/-/pipelines/2",
                },
            ]
        )
    )
    checks = gitlab.discover_head("5", "new", cwd=tmp_path, fetch_logs=False)
    assert len(checks) == 1
    assert checks[0].bucket == "success"
    assert checks[0].details_url == "https://gitlab.com/g/p/-/pipelines/2"


@patch("src.activities.ci_watch.gitlab.subprocess.run")
def test_discover_head_no_match_returns_empty(run: Any, tmp_path: Path) -> None:
    run.return_value = _completed(json.dumps([{"id": 1, "sha": "old", "status": "success"}]))
    assert gitlab.discover_head("5", "new", cwd=tmp_path, fetch_logs=False) == []


@patch("src.activities.ci_watch.gitlab.subprocess.run")
def test_discover_head_manual_job_pipeline_is_success(run: Any, tmp_path: Path) -> None:
    # A pipeline can be `success` overall even with a `manual` job that never
    # ran — gating reads the pipeline's own status, not a per-job fan-out.
    run.return_value = _completed(json.dumps([{"id": 1, "sha": "new", "status": "success"}]))
    checks = gitlab.discover_head("5", "new", cwd=tmp_path, fetch_logs=False)
    assert checks[0].bucket == "success"


@patch("src.activities.ci_watch.gitlab.job_trace")
@patch("src.activities.ci_watch.gitlab.subprocess.run")
def test_discover_head_failure_fetches_failed_job_logs_only(
    run: Any, trace: Any, tmp_path: Path
) -> None:
    run.side_effect = [
        _completed(json.dumps([{"id": 1, "sha": "new", "status": "failed"}])),
        _completed(
            json.dumps(
                [
                    {"id": 10, "name": "test", "status": "failed", "allow_failure": False},
                    {"id": 11, "name": "lint", "status": "failed", "allow_failure": True},
                    {"id": 12, "name": "build", "status": "success"},
                ]
            )
        ),
    ]
    trace.return_value = "trace output"
    checks = gitlab.discover_head("5", "new", cwd=tmp_path, fetch_logs=True)
    assert checks[0].bucket == "failure"
    assert "trace output" in checks[0].log_excerpt
    # allow_failure job and the passing job must not have their trace pulled
    trace.assert_called_once_with(10, cwd=tmp_path)


@patch("src.activities.ci_watch.gitlab.job_trace")
@patch("src.activities.ci_watch.gitlab.subprocess.run")
def test_discover_head_failure_caps_failed_job_logs(run: Any, trace: Any, tmp_path: Path) -> None:
    failing = [
        {"id": 100 + i, "name": f"job{i}", "status": "failed", "allow_failure": False}
        for i in range(8)
    ]
    run.side_effect = [
        _completed(json.dumps([{"id": 1, "sha": "new", "status": "failed"}])),
        _completed(json.dumps(failing)),
    ]
    trace.return_value = "trace output"
    gitlab.discover_head("5", "new", cwd=tmp_path, fetch_logs=True)
    assert trace.call_count == gitlab._MAX_FAILED_JOB_LOGS == 5
