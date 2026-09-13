"""FastStream consumer for Sentry webhook ingestion (Stage 2).

Consumes raw Sentry webhook payloads from the ``jeanclode.events.sentry.webhooks``
Redis Stream, resolves the installation to an Organization/Repository, and creates
or updates Issue records in the database.

Ref: ADR-004 — Ingestion Pipeline Stage 2
"""

import json
import logging
from datetime import datetime
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_create_issue,
    db_get_issue_by_sentry_id,
    db_get_org_by_installation_id,
    db_get_repository_by_org_and_external_id,
    db_update_issue,
)
from api.models.issues import Issue
from api.sse.publishers import publish_issue_event

logger = logging.getLogger(__name__)

router = RedisRouter()

# Webhook actions we care about
ACCEPTED_ACTIONS = {"created", "regression", "resolved", "unresolved"}


def _parse_payload(raw: bytes) -> dict[str, Any] | None:
    """Parse raw webhook bytes into a dict. Returns None on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Malformed webhook payload, discarding")
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string from Sentry."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


@router.subscriber(
    stream=StreamSub("jeanclode.events.sentry.webhooks", group="jeanclode", consumer="worker-1")
)
async def consume_sentry_webhook(payload: dict[str, Any]) -> None:
    """Consume a Sentry webhook from the Redis Stream.

    Steps:
        1. Validate action is one we handle
        2. Resolve installation UUID → Organization
        3. Resolve Sentry project ID → Repository
        4. Create or update the Issue record
        5. Emit SSE event for frontend
    """

    action = payload.get("action")
    if action not in ACCEPTED_ACTIONS:
        logger.debug(f"Ignoring webhook action: {action}")
        return

    # Extract installation UUID
    installation_uuid = payload.get("installation", {}).get("uuid")
    if not installation_uuid:
        logger.warning("Webhook missing installation.uuid, discarding")
        return

    # Extract issue data from the payload
    issue_data = payload.get("data", {}).get("issue", {})
    if not issue_data:
        logger.warning("Webhook missing data.issue, discarding")
        return

    sentry_project_id_str = str(issue_data.get("project", {}).get("id", ""))
    if not sentry_project_id_str:
        logger.warning("Webhook missing project ID, discarding")
        return

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    # Resolve Organization
    with db_plugin.session() as db:
        org = db_get_org_by_installation_id(db, installation_uuid)
        if not org:
            logger.warning(f"Unknown installation UUID: {installation_uuid}, discarding")
            return
        org_id = org.id
        workspace_id = org.workspace_id

    # Resolve Repository
    with db_plugin.session() as db:
        repository = db_get_repository_by_org_and_external_id(db, org_id, sentry_project_id_str)
        if not repository:
            logger.warning(
                f"Unknown repository {sentry_project_id_str} for org {org_id}, discarding"
            )
            return
        repository_pk = repository.id

    sentry_issue_id = str(issue_data.get("id", ""))
    title = issue_data.get("title", "Untitled")
    culprit = issue_data.get("culprit")
    level = issue_data.get("level", "error")
    first_seen = _parse_datetime(issue_data.get("firstSeen"))
    last_seen = _parse_datetime(issue_data.get("lastSeen"))
    event_count = int(issue_data.get("count") or 0)
    issue_url = issue_data.get("permalink")

    # Create or update issue
    issue: Issue | None = None
    sse_action = ""
    with db_plugin.session() as db:
        existing = db_get_issue_by_sentry_id(db, repository_pk, sentry_issue_id)

        if existing is None and action != "resolved":
            issue = db_create_issue(
                db=db,
                repository_id=repository_pk,
                external_id=sentry_issue_id,
                title=title,
                culprit=culprit,
                level=level,
                first_seen=first_seen,
                last_seen=last_seen,
                event_count=event_count,
                issue_url=issue_url,
                status="unresolved",
            )
            sse_action = "created"
            logger.info(f"Created issue {issue.id} (sentry:{sentry_issue_id})")
        elif existing is None:
            logger.debug(f"Ignoring resolved webhook for unknown issue {sentry_issue_id}")
        else:
            update_fields: dict[str, Any] = {
                "title": title,
                "culprit": culprit,
                "level": level,
                "last_seen": last_seen,
                "event_count": event_count,
            }
            # Set external status based on webhook action
            if action == "resolved":
                update_fields["status"] = "resolved"
            elif action in ("created", "regression", "unresolved"):
                update_fields["status"] = "unresolved"
            issue = db_update_issue(db, existing.id, **update_fields)
            sse_action = "updated"
            logger.info(f"Updated issue {existing.id} (sentry:{sentry_issue_id})")

    # Publish SSE event via Redis for cross-instance fan-out
    if issue:
        await publish_issue_event(
            workspace_id=str(workspace_id) if workspace_id else "",
            action=sse_action,
            payload={
                "issue_id": str(issue.id),
                "triage_result": issue.triage_result,
                "title": issue.title,
            },
        )
