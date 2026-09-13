"""Shared utilities for GitLab sync handlers."""

from datetime import datetime


def parse_gitlab_datetime(value: str | None) -> datetime | None:
    """Parse a GitLab ISO 8601 datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
