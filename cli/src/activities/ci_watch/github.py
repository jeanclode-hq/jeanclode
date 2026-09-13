"""GitHub CI discovery — Checks API + legacy Status API, via `gh`.

Both are queried and merged: modern checks (Actions, most integrations) live
in the Checks API, older/third-party CI in the legacy Status API. `gh`
infers `{owner}/{repo}` from the git remote via its own placeholder
substitution, so no repo slug is threaded through these functions.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from src.activities.ci_watch.schemas import CheckBucket, CheckResult, truncate_log

_BLOCKED_CONCLUSIONS = {"action_required", "stale", "skipped", "neutral"}
_FAILURE_CONCLUSIONS = {"failure", "cancelled", "timed_out"}

# GitHub's check-runs/status list endpoints default to 30 per page; 100 is
# the max. Same failure mode verified live on GitLab's job list (a 25-job
# pipeline silently lost its one real failure past the default page of 20).
_MAX_PER_PAGE = 100

# An Actions check-run's details_url looks like
# https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id} —
# verified against a live check-run. Non-Actions apps (CodeQL, third-party
# CI) point elsewhere and simply won't match.
_ACTIONS_RUN_JOB_RE = re.compile(r"/actions/runs/(?P<run_id>\d+)/job/(?P<job_id>\d+)")


def _run_gh(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _api_json(args: list[str], *, cwd: Path) -> Any | None:
    """Run `gh api ...` and return parsed JSON, or None on any failure."""
    proc = _run_gh(args, cwd=cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _bucket_for_conclusion(conclusion: str) -> CheckBucket:
    if conclusion == "success":
        return "success"
    if conclusion in _FAILURE_CONCLUSIONS:
        return "failure"
    if conclusion in _BLOCKED_CONCLUSIONS:
        return "blocked"
    return "running"


def list_check_runs(ref: str, *, cwd: Path) -> list[dict[str, Any]]:
    """Return every check-run on `ref` (a SHA or branch name)."""
    data = _api_json(
        ["api", f"repos/{{owner}}/{{repo}}/commits/{ref}/check-runs?per_page={_MAX_PER_PAGE}"],
        cwd=cwd,
    )
    return data.get("check_runs", []) if isinstance(data, dict) else []


def combined_status(ref: str, *, cwd: Path) -> list[dict[str, Any]]:
    """Return every legacy Status API entry on `ref`."""
    data = _api_json(
        ["api", f"repos/{{owner}}/{{repo}}/commits/{ref}/status?per_page={_MAX_PER_PAGE}"], cwd=cwd
    )
    return data.get("statuses", []) if isinstance(data, dict) else []


def required_check_names(branch: str, *, cwd: Path) -> set[str] | None:
    """Best-effort: names of checks that actually block merging on `branch`.

    Requires admin-level read access to branch protection, which we may not
    have. Returns None on any failure (403/404/malformed) so the caller can
    fall back to treating every discovered check as required.
    """
    data = _api_json(
        ["api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection/required_status_checks"],
        cwd=cwd,
    )
    if not isinstance(data, dict):
        return None
    contexts = data.get("contexts") or []
    return set(contexts) if contexts else None


def failed_run_log(details_url: str, *, cwd: Path) -> str:
    """Best-effort failing-step log for a GitHub Actions check-run.

    Parses the run/job id straight out of the check-run's own `details_url`
    rather than matching by name: a check-run's `name` is per-job (e.g.
    "test-backend"), but `gh run list` only exposes per-*workflow* names
    (e.g. "CI") — they never match. `gh run view --log-failed` then handles
    the zip/redirect dance the raw Actions logs API otherwise needs.
    Non-Actions checks (CodeQL, third-party apps) have a different
    `details_url` shape and simply return "".
    """
    match = _ACTIONS_RUN_JOB_RE.search(details_url)
    if not match:
        return ""
    proc = _run_gh(
        ["run", "view", match["run_id"], "--job", match["job_id"], "--log-failed"],
        cwd=cwd,
    )
    if proc.returncode != 0:
        return ""
    return truncate_log(proc.stdout)


def discover(ref: str, *, cwd: Path, fetch_logs: bool = True) -> list[CheckResult]:
    """Discover every check on `ref` (the head commit's SHA) across both
    GitHub signal APIs.

    Required-check weighting is applied by the caller (`poll.py`), which
    knows the actual PR branch — required-status-checks are keyed by branch,
    not SHA, so that step can't happen in here.
    """
    results: list[CheckResult] = []
    for run in list_check_runs(ref, cwd=cwd):
        status = run.get("status")
        conclusion = run.get("conclusion") or ""
        bucket: CheckBucket = (
            "running" if status != "completed" else _bucket_for_conclusion(conclusion)
        )
        name = run.get("name", "")
        details_url = run.get("details_url", "")
        log_excerpt = ""
        if fetch_logs and bucket == "failure":
            log_excerpt = failed_run_log(details_url, cwd=cwd)
        results.append(
            CheckResult(
                name=name,
                bucket=bucket,
                raw_conclusion=conclusion or status or "",
                log_excerpt=log_excerpt,
                details_url=details_url,
            )
        )
    status_buckets: dict[str, CheckBucket] = {"success": "success", "pending": "running"}
    for status_entry in combined_status(ref, cwd=cwd):
        state = status_entry.get("state", "")
        status_bucket = status_buckets.get(state, "failure")
        results.append(
            CheckResult(
                name=status_entry.get("context", ""),
                bucket=status_bucket,
                raw_conclusion=state,
                details_url=status_entry.get("target_url", ""),
            )
        )
    return results
