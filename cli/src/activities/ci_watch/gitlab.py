"""GitLab CI discovery — MR pipelines + jobs, via `glab`.

Unlike GitHub's many independent checks, a GitLab MR has one pipeline whose
own `status` is authoritative (a job can be `allow_failure` without failing
the pipeline, or `manual` without blocking it) — gating reads the pipeline
status, not a per-job fan-out. Jobs are only fetched for failing-job traces.

`glab` substitutes `:id` from the git remote and already reads `GITLAB_HOST`
for self-hosted instances, so no host/project-id resolution is needed here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from src.activities.ci_watch.schemas import CheckBucket, CheckResult, truncate_log

# Traces for the first N non-allow_failure failing jobs are fed back to the
# fixer. Each is tail-capped (truncate_log), so 5 stays well within the
# agent's context while covering a pipeline where a shared cause broke
# several jobs at once.
_MAX_FAILED_JOB_LOGS = 5
# GitLab's list endpoints default to 20 per page — verified live against a
# 25-job pipeline where the unpaginated call silently dropped the one
# non-allow_failure failing job. 100 is GitLab's max per_page.
_MAX_PER_PAGE = 100

# Anything not in these two sets (created, pending, running, ...) falls
# through to the "running" bucket — the safe default mid-pipeline.
_BLOCKED_STATUSES = {"skipped", "manual"}
_FAILURE_STATUSES = {"failed", "canceled"}


def _run_glab(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["glab", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _api_json(args: list[str], *, cwd: Path) -> Any | None:
    """Run `glab api ...` and return parsed JSON, or None on any failure."""
    proc = _run_glab(args, cwd=cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _bucket_for_status(status: str) -> CheckBucket:
    if status == "success":
        return "success"
    if status in _FAILURE_STATUSES:
        return "failure"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    return "running"


def mr_pipelines(mr_iid: str, *, cwd: Path) -> list[dict[str, Any]]:
    data = _api_json(
        ["api", f"projects/:id/merge_requests/{mr_iid}/pipelines?per_page={_MAX_PER_PAGE}"], cwd=cwd
    )
    return data if isinstance(data, list) else []


def pipeline_jobs(pipeline_id: int, *, cwd: Path) -> list[dict[str, Any]]:
    data = _api_json(
        ["api", f"projects/:id/pipelines/{pipeline_id}/jobs?per_page={_MAX_PER_PAGE}"], cwd=cwd
    )
    return data if isinstance(data, list) else []


def job_trace(job_id: int, *, cwd: Path) -> str:
    """Plain-text job trace — no redirect/zip dance needed, unlike GitHub."""
    proc = _run_glab(["api", f"projects/:id/jobs/{job_id}/trace"], cwd=cwd)
    if proc.returncode != 0:
        return ""
    return truncate_log(proc.stdout)


def discover_head(
    mr_iid: str, head_sha: str, *, cwd: Path, fetch_logs: bool = True
) -> list[CheckResult]:
    """Discover the pipeline for the MR's head commit specifically.

    Matches by SHA against the MR's own pipelines (not just "latest") so a
    second push during a retry can't read a stale result from a superseded
    commit.
    """
    pipelines = mr_pipelines(mr_iid, cwd=cwd)
    pipeline = next((p for p in pipelines if p.get("sha") == head_sha), None)
    if pipeline is None:
        return []
    return _discover_from_pipeline(pipeline, cwd=cwd, fetch_logs=fetch_logs)


def _discover_from_pipeline(
    pipeline: dict[str, Any], *, cwd: Path, fetch_logs: bool
) -> list[CheckResult]:
    status = pipeline.get("status", "")
    bucket = _bucket_for_status(status)
    result = CheckResult(
        name="pipeline",
        bucket=bucket,
        raw_conclusion=status,
        details_url=pipeline.get("web_url", ""),
    )
    if fetch_logs and bucket == "failure":
        jobs = pipeline_jobs(pipeline["id"], cwd=cwd)
        failed_jobs = [
            j for j in jobs if j.get("status") == "failed" and not j.get("allow_failure")
        ][:_MAX_FAILED_JOB_LOGS]
        excerpts = [
            f"--- job: {j.get('name', '?')} ---\n{job_trace(j['id'], cwd=cwd)}" for j in failed_jobs
        ]
        result.log_excerpt = "\n\n".join(excerpts)
    return [result]
