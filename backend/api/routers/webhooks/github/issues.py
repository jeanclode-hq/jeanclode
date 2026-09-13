"""GitHub issues webhook handler — upsert issue inline, dispatch, emit SSE."""

import logging
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.issue import db_create_issue, db_get_issue_by_sentry_id, db_update_issue
from api.database.repository import db_get_repository_by_external_id
from api.routers.webhooks.git_dispatch import maybe_dispatch_issue_workflow
from api.sse.publishers import publish_issue_event

from ..utils import WebhookResponse
from .utils import parse_github_datetime

logger = logging.getLogger(__name__)


class _Upserted(NamedTuple):
    """What the SSE and dispatch phases need once the session is closed."""

    issue_id: UUID
    title: str
    status: str
    issue_url: str
    created: bool
    workspace_id: str | None
    org_id: UUID
    repo_name: str


async def handle_issue_event(event: dict) -> WebhookResponse:
    """Handle issues webhook — upsert issue inline and emit SSE.

    Handles actions: opened, edited, closed, reopened, deleted.
    """
    action = event.get("action", "")
    issue_data = event.get("issue", {})
    repo_data = event.get("repository", {})

    external_id = str(issue_data.get("number", ""))
    status = issue_data.get("state", "open")

    def _upsert(db: Session) -> WebhookResponse | _Upserted:
        # Find repository by GitHub's numeric ID
        external_repo_id = str(repo_data.get("id", ""))
        repository = db_get_repository_by_external_id(db, external_repo_id)
        if not repository:
            return WebhookResponse(
                message=f"Repository {repo_data.get('full_name', external_repo_id)} not tracked",
                processed=False,
            )

        title = issue_data.get("title", "")
        author = issue_data.get("user", {}).get("login", "")
        issue_url = issue_data.get("html_url", "")
        comments = issue_data.get("comments", 0)
        created_at = parse_github_datetime(issue_data.get("created_at"))
        updated_at = parse_github_datetime(issue_data.get("updated_at"))

        # Check if issue already exists
        existing = db_get_issue_by_sentry_id(db, repository.id, external_id)

        if action == "deleted" and existing:
            # Soft delete: mark as closed
            db_update_issue(db, existing.id, status="closed")
            logger.info(f"Issue #{external_id} deleted → closed (repo {repository.name})")
            return WebhookResponse(message=f"Issue #{external_id} deleted", processed=True)

        if existing:
            # Update existing issue
            db_update_issue(
                db,
                existing.id,
                title=title,
                status=status,
                last_seen=updated_at,
                event_count=comments,
            )
            issue = existing
            created = False
        else:
            # Create new issue
            issue = db_create_issue(
                db=db,
                repository_id=repository.id,
                external_id=external_id,
                title=title,
                level="info",
                status=status,
                author=author,
                issue_url=issue_url,
                event_count=comments,
                first_seen=created_at,
                last_seen=updated_at,
            )
            created = True

        workspace_id = None
        if repository.organization and repository.organization.workspace_id:
            workspace_id = str(repository.organization.workspace_id)

        return _Upserted(
            issue_id=issue.id,
            title=issue.title,
            status=issue.status,
            issue_url=issue.issue_url,
            created=created,
            workspace_id=workspace_id,
            org_id=repository.org_id,
            repo_name=repository.name,
        )

    outcome = await run_in_session(_upsert)
    if isinstance(outcome, WebhookResponse):
        return outcome

    # Emit SSE
    if action != "labeled" and action != "unlabeled" and outcome.workspace_id:
        await publish_issue_event(
            workspace_id=outcome.workspace_id,
            action="created" if outcome.created else "updated",
            payload={
                "issue_id": str(outcome.issue_id),
                "title": outcome.title,
                "status": outcome.status,
                "issue_url": outcome.issue_url,
            },
        )

    # Webhook-driven issue-resolve dispatch. Skipped for closed issues —
    # resolving a closed issue makes no sense.
    if status == "open" and action in ("opened", "labeled"):
        label_added: str | None = None
        if action == "labeled":
            label_obj = event.get("label") or {}
            label_added = label_obj.get("name") if isinstance(label_obj, dict) else None
        await maybe_dispatch_issue_workflow(
            issue_id=outcome.issue_id,
            org_id=outcome.org_id,
            provider="github",
            action=action,
            label_added=label_added,
        )

    logger.info(f"Issue #{external_id} {action} → {status} (repo {outcome.repo_name})")
    return WebhookResponse(
        message=f"Issue #{external_id} {action} → {status}",
        processed=True,
    )
