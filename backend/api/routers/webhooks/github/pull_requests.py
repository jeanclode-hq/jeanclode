"""GitHub pull_request webhook handler (handled inline)."""

import logging
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.pull_request import db_upsert_pull_request, resolve_workspace_id
from api.database.repository import db_get_repository_by_external_id
from api.routers.webhooks.git_dispatch import maybe_dispatch_pr_workflows
from api.sse.publishers import publish_pull_request_event

from ..utils import WebhookResponse
from .utils import added_labels, parse_github_datetime, resolve_pr_state

logger = logging.getLogger(__name__)


class _Upserted(NamedTuple):
    """What the SSE and dispatch phases need once the session is closed."""

    pr_id: UUID
    pr_number: int
    created: bool
    workspace_id: str | None
    sse_payload: dict
    org_id: UUID
    repo_name: str


async def handle_pull_request_event(event: dict) -> WebhookResponse:
    """Handle pull_request webhook — upsert PR inline, evaluate triggers, emit SSE.

    Handled inline: validate + upsert + SSE in the request,
    then evaluate the org's review/summary triggers and queue an
    execution per match.
    """
    action = event.get("action", "")
    pr_data = event.get("pull_request", {})
    repo_data = event.get("repository", {})
    state = resolve_pr_state(pr_data)

    def _upsert(db: Session) -> WebhookResponse | _Upserted:
        # Find repository by GitHub's numeric ID
        external_repo_id = str(repo_data.get("id", ""))
        repository = db_get_repository_by_external_id(db, external_repo_id)
        if not repository:
            return WebhookResponse(
                message=f"Repository {repo_data.get('full_name', external_repo_id)} not tracked",
                processed=False,
            )

        # Upsert PR — author from pull_request.user.login (GitHub)
        pr, created = db_upsert_pull_request(
            db,
            repository_id=repository.id,
            pr_number=pr_data.get("number", 0),
            title=pr_data.get("title", ""),
            author=pr_data.get("user", {}).get("login", ""),
            state=state,
            pr_url=pr_data.get("html_url", ""),
            head_branch=pr_data.get("head", {}).get("ref", ""),
            base_branch=pr_data.get("base", {}).get("ref", ""),
            external_pr_id=str(pr_data.get("id", "")),
            head_sha=pr_data.get("head", {}).get("sha"),
            created_at=parse_github_datetime(pr_data.get("created_at")),
        )

        return _Upserted(
            pr_id=pr.id,
            pr_number=pr.pr_number,
            created=created,
            workspace_id=resolve_workspace_id(pr),
            sse_payload={
                "pull_request_id": str(pr.id),
                "pr_number": pr.pr_number,
                "state": pr.state,
                "pr_url": pr.pr_url,
                "title": pr.title,
                "head_branch": pr.head_branch,
                "author": pr.author,
            },
            org_id=repository.org_id,
            repo_name=repository.name,
        )

    outcome = await run_in_session(_upsert)
    if isinstance(outcome, WebhookResponse):
        return outcome

    # Emit SSE (skip noise from synchronize — only state/title changes matter)
    if action != "synchronize" and outcome.workspace_id:
        await publish_pull_request_event(
            workspace_id=outcome.workspace_id,
            action="created" if outcome.created else "updated",
            payload=outcome.sse_payload,
        )

    # Webhook-driven workflow dispatch — REVIEW + SUMMARY, label-gated.
    # Skipped for closed/merged PRs since downstream containers can't post
    # on a closed PR cleanly.
    if state.value == "open":
        for label_added in added_labels(event, action, pr_data):
            await maybe_dispatch_pr_workflows(
                pr_id=outcome.pr_id,
                org_id=outcome.org_id,
                provider="github",
                action="labeled",
                label_added=label_added,
            )

    logger.info(f"PR #{outcome.pr_number} {action} → {state.value} (repo {outcome.repo_name})")
    return WebhookResponse(
        message=f"PR #{outcome.pr_number} {action} → {state.value}",
        processed=True,
    )
