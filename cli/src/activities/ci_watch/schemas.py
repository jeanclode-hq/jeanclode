"""ci-watch schemas — a normalized view of "did CI pass" across providers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Normalized across GitHub check-runs/statuses and GitLab pipeline status.
# "blocked" (no CI, action_required, skipped, ...) is a pass-through, not a
# failure — see poll.py's gating logic.
CheckBucket = Literal["success", "failure", "running", "blocked"]


class CheckResult(BaseModel):
    """One check/pipeline discovered on a commit."""

    name: str
    bucket: CheckBucket
    raw_conclusion: str = ""
    required: bool = True
    log_excerpt: str = ""
    details_url: str = ""


class CiWatchResult(BaseModel):
    """The outcome of one ci-watch call."""

    outcome: Literal["finish", "failure"]
    reason: str
    checks: list[CheckResult] = Field(default_factory=list)
    summary_text: str


# Log excerpts are handed back to the agent as tool output — cap so a huge
# failing step doesn't blow out the agent's context. Shared by github.py and
# gitlab.py, whose failing-step/job-trace logs are otherwise unrelated.
LOG_EXCERPT_CHARS = 4000


def truncate_log(text: str) -> str:
    """Keep only the tail — the failure is almost always at the end of the log, not the start."""
    return text[-LOG_EXCERPT_CHARS:] if len(text) > LOG_EXCERPT_CHARS else text
