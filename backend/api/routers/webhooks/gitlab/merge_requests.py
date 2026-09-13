"""GitLab merge_request webhook handler (handled inline)."""

import logging
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.pull_request import db_upsert_pull_request, resolve_workspace_id
from api.database.repository import db_get_repository_by_external_id
from api.models.pull_requests import PRState
from api.routers.sources.gitlab.utils import parse_gitlab_datetime
from api.routers.webhooks.git_dispatch import maybe_dispatch_pr_workflows
from api.sse.publishers import publish_pull_request_event

from ..utils import WebhookResponse

logger = logging.getLogger(__name__)

# GitLab MR state → PRState mapping
_GITLAB_STATE_MAP = {
    "merged": PRState.MERGED,
    "closed": PRState.CLOSED,
    "opened": PRState.OPEN,
    "reopened": PRState.OPEN,
}


def _resolve_mr_state(mr_data: dict) -> PRState:
    """Map GitLab MR state to PRState."""
    state = mr_data.get("state", "opened")
    return _GITLAB_STATE_MAP.get(state, PRState.OPEN)


def _normalize_action(action: str, changes: dict) -> tuple[str, list[str]]:
    """Normalize a GitLab MR action to the GitHub vocabulary used by the
    trigger evaluator.

    Returns ``(normalized_action, added_label_names)`` — the latter only
    populated when the event represents one or more newly attached
    labels (so the evaluator can fire ON_LABEL once per added label).

    GitLab fires ``update`` for both new commits *and* metadata edits
    (labels, title, …). We disambiguate via ``changes``: if labels
    changed we treat it as a label event; otherwise we treat it as a
    push (synchronize). This is the same heuristic the predecessor project used.
    """
    if action == "open":
        return "opened", []

    if action == "update":
        labels_change = changes.get("labels") if isinstance(changes, dict) else None
        if isinstance(labels_change, dict):
            previous = {lbl.get("title", "") for lbl in labels_change.get("previous", [])}
            current = {lbl.get("title", "") for lbl in labels_change.get("current", [])}
            added = sorted(current - previous)
            if added:
                return "labeled", added
        return "synchronize", []

    # close / merge / reopen — out of scope for auto-trigger.
    return action, []


class _Upserted(NamedTuple):
    """What the SSE and dispatch phases need once the session is closed."""

    pr_id: UUID
    pr_number: int
    created: bool
    workspace_id: str | None
    sse_payload: dict
    org_id: UUID
    repo_name: str


async def handle_merge_request_event(event: dict) -> WebhookResponse:
    """Handle merge_request webhook — upsert PR inline and emit SSE."""
    mr_data = event.get("object_attributes", {})
    project_data = event.get("project", {})
    user_data = event.get("user", {})
    action = mr_data.get("action", mr_data.get("state", ""))
    state = _resolve_mr_state(mr_data)

    def _upsert(db: Session) -> WebhookResponse | _Upserted:
        # Find repository by GitLab project ID
        external_repo_id = str(project_data.get("id", ""))
        repository = db_get_repository_by_external_id(db, external_repo_id)
        if not repository:
            return WebhookResponse(
                message=f"Repository {project_data.get('path_with_namespace', external_repo_id)} not tracked",
                processed=False,
            )

        # Upsert PR — author from top-level event.user.username (GitLab)
        pr, created = db_upsert_pull_request(
            db,
            repository_id=repository.id,
            pr_number=mr_data.get("iid", 0),
            title=mr_data.get("title", ""),
            author=user_data.get("username", ""),
            state=state,
            pr_url=mr_data.get("url", ""),
            head_branch=mr_data.get("source_branch", ""),
            base_branch=mr_data.get("target_branch", ""),
            external_pr_id=str(mr_data.get("id", "")),
            head_sha=mr_data.get("last_commit", {}).get("id"),
            created_at=parse_gitlab_datetime(mr_data.get("created_at")),
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

    # Emit SSE (skip generic "update" noise unless title changed)
    should_emit = action in ("open", "close", "merge", "reopen") or outcome.created
    if should_emit and outcome.workspace_id:
        await publish_pull_request_event(
            workspace_id=outcome.workspace_id,
            action="created" if outcome.created else "updated",
            payload=outcome.sse_payload,
        )

    # Webhook-driven workflow dispatch — REVIEW + SUMMARY, label-gated.
    # Only fires for open MRs, and only when a label was just added.
    if state == PRState.OPEN:
        normalized_action, added_labels = _normalize_action(action, event.get("changes") or {})
        if normalized_action == "labeled" and added_labels:
            # Fire once per added label — the evaluator only matches the
            # workflow whose label was added, so iterating is cheap.
            for added in added_labels:
                await maybe_dispatch_pr_workflows(
                    pr_id=outcome.pr_id,
                    org_id=outcome.org_id,
                    provider="gitlab",
                    action="labeled",
                    label_added=added,
                )

    logger.info(f"MR !{outcome.pr_number} {action} → {state.value} (repo {outcome.repo_name})")
    return WebhookResponse(
        message=f"MR !{outcome.pr_number} {action} → {state.value}",
        processed=True,
    )
