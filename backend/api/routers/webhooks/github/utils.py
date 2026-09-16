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


def added_labels(event: dict, action: str, item: dict) -> list[str]:
    """Labels this event attaches: the one ``labeled`` names, or all an ``opened`` item starts with."""
    if action == "labeled":
        label_obj = event.get("label") or {}
        name = label_obj.get("name") if isinstance(label_obj, dict) else None
        return [name] if name else []
    if action == "opened":
        # GitHub doesn't document a `labeled` delivery for labels set at creation.
        labels = item.get("labels") or []
        return [lbl["name"] for lbl in labels if isinstance(lbl, dict) and lbl.get("name")]
    return []
