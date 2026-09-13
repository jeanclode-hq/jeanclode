"""ci-watch gating state machine — dispatches to github.py / gitlab.py.

One deterministic function, `check_ci`, used both from the fixer agent's
Stop hook (`src.agents.hooks.require_ci_pass_hook`) and as the runner's own
authoritative post-session gate. Calling it twice with the same inputs must
always agree — that's the whole point of sharing one implementation.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Literal

from src.activities.ci_watch import github, gitlab
from src.activities.ci_watch.schemas import CheckResult, CiWatchResult
from src.activities.git.pr import pr_id
from src.activities.git.schemas import PRRef

# A few short polls a few seconds apart before concluding "no CI configured"
# — webhook delivery and runner scheduling both have real lag, so an empty
# result immediately after push is not conclusive.
_SETTLE_POLLS = 3
_SETTLE_INTERVAL_SECONDS = 5.0

# Bounded wait for still-running checks within a single ci-watch call. A
# call that times out with checks still running is treated as a failure for
# gating purposes, same as a real one — the caller just checks again later
# rather than one call blocking indefinitely.
_WAIT_TIMEOUT_SECONDS = 480.0
_WAIT_POLL_INTERVAL_SECONDS = 15.0


def _head_sha(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _discover_head(pr: PRRef, head_sha: str, *, cwd: Path, fetch_logs: bool) -> list[CheckResult]:
    if pr.platform == "github":
        return github.discover(head_sha, cwd=cwd, fetch_logs=fetch_logs)
    return gitlab.discover_head(pr_id(pr), head_sha, cwd=cwd, fetch_logs=fetch_logs)


def _apply_required_weighting(pr: PRRef, checks: list[CheckResult], *, cwd: Path) -> None:
    """GitHub only: mark non-required checks so gating can ignore their
    failures. Best-effort — leaves every check `required=True` (the safe
    default already set by the schema) if branch protection isn't readable.
    """
    if pr.platform != "github":
        return
    required = github.required_check_names(pr.branch, cwd=cwd)
    if required is None:
        return
    for check in checks:
        check.required = check.name in required


def _summarize(checks: list[CheckResult], outcome: str, reason: str) -> str:
    lines = [f"ci-watch: {outcome} — {reason}", ""]
    for c in checks:
        marker = "required" if c.required else "informational"
        lines.append(f"- [{c.bucket}/{marker}] {c.name} ({c.raw_conclusion or 'n/a'})")
        if c.log_excerpt:
            lines.append(f"  log excerpt:\n{c.log_excerpt}")
    return "\n".join(lines)


def check_ci(pr: PRRef, *, cwd: Path) -> CiWatchResult:
    """Resolve the head commit and discover every check on it, waiting for
    completion.

    This function only answers "is CI currently green" — it doesn't decide
    whether a failure is this change's fault. Within the fixer's own Stop
    hook that's what drives the fix-and-recheck loop (see
    src.agents.hooks); after that hook's retry budget is spent, or from the
    runner's own post-session call, a still-red result is informational
    only — the PR gets labeled regardless.

    Same function backs the fixer agent's Stop hook and the runner's own
    post-session check (see module docstring).
    """
    head_sha = _head_sha(cwd)

    checks = _discover_head(pr, head_sha, cwd=cwd, fetch_logs=False)
    settled = 0
    while not checks and settled < _SETTLE_POLLS:
        time.sleep(_SETTLE_INTERVAL_SECONDS)
        checks = _discover_head(pr, head_sha, cwd=cwd, fetch_logs=False)
        settled += 1

    if not checks:
        return CiWatchResult(
            outcome="finish",
            reason="no CI configured on this commit",
            summary_text="No CI checks found on this commit — nothing to verify against.",
        )

    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while any(c.bucket == "running" for c in checks):
        if time.monotonic() >= deadline:
            break
        time.sleep(_WAIT_POLL_INTERVAL_SECONDS)
        checks = _discover_head(pr, head_sha, cwd=cwd, fetch_logs=False)

    still_running = [c for c in checks if c.bucket == "running"]
    any_failed = any(c.bucket == "failure" for c in checks)

    # Only pay for failing-step logs and required-check weighting when
    # there's actually something to explain — every green (or still-running)
    # result skips both extra round trips.
    if any_failed:
        checks = _discover_head(pr, head_sha, cwd=cwd, fetch_logs=True)
        _apply_required_weighting(pr, checks, cwd=cwd)

    failing_required = [c for c in checks if c.bucket == "failure" and c.required]

    outcome: Literal["finish", "failure"]
    if still_running:
        outcome = "failure"
        reason = f"ci-watch's own wait timed out with {len(still_running)} check(s) still running"
    elif failing_required:
        outcome = "failure"
        names = ", ".join(c.name for c in failing_required)
        reason = f"{len(failing_required)} required check(s) failed: {names}"
    else:
        outcome = "finish"
        reason = "all required checks passed (or nothing to verify against)"

    return CiWatchResult(
        outcome=outcome,
        reason=reason,
        checks=checks,
        summary_text=_summarize(checks, outcome, reason),
    )
