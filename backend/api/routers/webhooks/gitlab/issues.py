"""GitLab issue webhook handler — upsert issue inline, dispatch, emit SSE.

Mirrors :mod:`api.routers.webhooks.github.issues`. GitLab's action
vocabulary (``open`` / ``update`` / ``close`` / ``reopen``) is normalized
to GitHub's (``opened`` / ``labeled``) before the dispatch evaluation, so
the trigger rule lives in one place (``git_dispatch``).
"""

import logging
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.issue import db_create_issue, db_get_issue_by_sentry_id, db_update_issue
from api.database.repository import db_get_repository_by_external_id
from api.routers.webhooks.git_dispatch import maybe_dispatch_issue_workflow
from api.sse.publishers import publish_issue_event

from ..utils import WebhookResponse

logger = logging.getLogger(__name__)

# GitLab issue state → Issue.status (DB uses the GitHub vocabulary)
_GITLAB_ISSUE_STATE_MAP = {
    "opened": "open",
    "closed": "closed",
}


def _parse_gitlab_datetime(value: str | None) -> datetime | None:
    """Parse a GitLab webhook datetime (``2021-03-12 12:00:00 UTC`` or ISO)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        return None


def _normalize_action(action: str, event: dict) -> tuple[str, list[str]]:
    """Normalize a GitLab issue action to the GitHub vocabulary used by
    the dispatch evaluator.

    Returns ``(normalized_action, added_label_names)`` — the latter only
    populated when the event represents one or more newly attached labels,
    or on ``open`` every label the issue starts with.
    GitLab fires ``update`` for any metadata edit; a label attach is
    detected via the ``changes.labels`` delta, same as the MR handler.
    """
    if action == "open":
        # Labels set at creation only ever show up here: GitLab sends no update for them.
        labels = event.get("labels") or event.get("object_attributes", {}).get("labels") or []
        return "opened", sorted(
            {lbl.get("title", "") for lbl in labels if isinstance(lbl, dict)} - {""}
        )

    if action == "update":
        changes = event.get("changes") or {}
        labels_change = changes.get("labels") if isinstance(changes, dict) else None
        if isinstance(labels_change, dict):
            previous = {lbl.get("title", "") for lbl in labels_change.get("previous", [])}
            current = {lbl.get("title", "") for lbl in labels_change.get("current", [])}
            added = sorted(current - previous)
            if added:
                return "labeled", added
        return "updated", []

    return action, []


class _Upserted(NamedTuple):
    """What the SSE and dispatch phases need once the session is closed."""

    issue_id: UUID
    created: bool
    workspace_id: str | None
    sse_payload: dict
    org_id: UUID
    repo_name: str


async def handle_issue_event(event: dict) -> WebhookResponse:
    """Handle a GitLab issue webhook — upsert issue, evaluate dispatch, emit SSE.

    Handles actions: open, update (incl. label changes), close, reopen.
    """
    issue_data = event.get("object_attributes", {})
    project_data = event.get("project", {})
    action = issue_data.get("action", "")
    external_id = str(issue_data.get("iid", ""))
    status = _GITLAB_ISSUE_STATE_MAP.get(issue_data.get("state", "opened"), "open")
    normalized_action, added_labels = _normalize_action(action, event)

    def _upsert(db: Session) -> WebhookResponse | _Upserted:
        # Find repository by GitLab project ID
        external_repo_id = str(project_data.get("id", ""))
        repository = db_get_repository_by_external_id(db, external_repo_id)
        if not repository:
            return WebhookResponse(
                message=f"Repository {project_data.get('path_with_namespace', external_repo_id)} not tracked",
                processed=False,
            )

        title = issue_data.get("title", "")
        author = (event.get("user") or {}).get("username", "")
        issue_url = issue_data.get("url", "")
        created_at = _parse_gitlab_datetime(issue_data.get("created_at"))
        updated_at = _parse_gitlab_datetime(issue_data.get("updated_at"))

        existing = db_get_issue_by_sentry_id(db, repository.id, external_id)
        if existing:
            db_update_issue(
                db,
                existing.id,
                title=title,
                status=status,
                last_seen=updated_at,
            )
            issue = existing
            created = False
        else:
            issue = db_create_issue(
                db=db,
                repository_id=repository.id,
                external_id=external_id,
                title=title,
                level="info",
                status=status,
                author=author,
                issue_url=issue_url,
                first_seen=created_at,
                last_seen=updated_at,
            )
            created = True

        workspace_id = None
        if repository.organization and repository.organization.workspace_id:
            workspace_id = str(repository.organization.workspace_id)

        return _Upserted(
            issue_id=issue.id,
            created=created,
            workspace_id=workspace_id,
            sse_payload={
                "issue_id": str(issue.id),
                "title": issue.title,
                "status": issue.status,
                "issue_url": issue.issue_url,
            },
            org_id=repository.org_id,
            repo_name=repository.name,
        )

    outcome = await run_in_session(_upsert)
    if isinstance(outcome, WebhookResponse):
        return outcome

    # Emit SSE (skip label-only updates, mirroring the GitHub handler)
    if normalized_action != "labeled" and outcome.workspace_id:
        await publish_issue_event(
            workspace_id=outcome.workspace_id,
            action="created" if outcome.created else "updated",
            payload=outcome.sse_payload,
        )

    # Webhook-driven issue-resolve dispatch. Skipped for closed issues —
    # resolving a closed issue makes no sense.
    if status == "open":
        for added in added_labels:
            await maybe_dispatch_issue_workflow(
                issue_id=outcome.issue_id,
                org_id=outcome.org_id,
                provider="gitlab",
                action="labeled",
                label_added=added,
            )

    logger.info(f"Issue #{external_id} {action} → {status} (repo {outcome.repo_name})")
    return WebhookResponse(
        message=f"Issue #{external_id} {action} → {status}",
        processed=True,
    )
