"""Shared utilities for GitHub webhook handlers."""

from datetime import datetime

from api.models.pull_requests import PRState


def parse_github_datetime(value: str | None) -> datetime | None:
    """Parse a GitHub ISO 8601 datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def resolve_pr_state(pr_data: dict) -> PRState:
    """Map GitHub PR fields to PRState."""
    if pr_data.get("merged"):
        return PRState.MERGED
    state = pr_data.get("state", "open")
    if state == "closed":
        return PRState.CLOSED
    return PRState.OPEN
