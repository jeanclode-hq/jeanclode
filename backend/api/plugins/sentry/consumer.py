"""FastStream consumers for the sentry plugin.

Subscribers:
- ``jeanclode.events.manual_dispatch`` — manual fix trigger
- ``jeanclode.sentry.execution.status`` — container watcher status updates
"""

import logging
import uuid
from typing import Any

from faststream.redis import RedisRouter, StreamSub
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload, selectinload

from api.context import get_current_app
from api.database.execution import db_link_execution_pull_requests, db_update_execution_status
from api.database.issue import db_update_issue
from api.database.organization import db_get_org_by_id
from api.database.pull_request import (
    db_get_pull_request,
    db_link_issue_pull_requests,
    db_upsert_pull_request,
)
from api.database.repository import db_get_related_repos
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PRState
from api.models.repositories import Repository, RepositoryMapping
from api.plugins.container.consumer_gate import status_consumer_gate
from api.plugins.container.dispatch_inputs import resolve_memory_workspace_id
from api.plugins.container.issue_resolve_result import normalize_web_url
from api.plugins.sentry.launch import launch_container
from api.sse.publishers import publish_execution_event

logger = logging.getLogger(__name__)

router = RedisRouter()


class ManualDispatchEvent(BaseModel):
    """Event published when a user manually triggers a fix."""

    issue_id: str
    execution_id: str
    organization_id: str


class ExecutionStatusMessage(BaseModel):
    """Status update from the container watcher."""

    execution_id: str
    status: str
    container_id: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    error_type: str | None = None
    result: dict[str, Any] | None = None
    timestamp: str | None = None


async def _publish_failure(
    execution_id: uuid.UUID,
    issue_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    """Publish an SSE failure event, resolving workspace_id from the org."""
    app = get_current_app()
    db_plugin = app.database
    workspace_id = ""
    if db_plugin:
        with db_plugin.session() as db:
            org = db_get_org_by_id(db, org_id)
            if org and org.workspace_id:
                workspace_id = str(org.workspace_id)
    await publish_execution_event(
        workspace_id=workspace_id,
        action="updated",
        payload={
            "execution_id": str(execution_id),
            "issue_id": str(issue_id),
            "status": ExecutionStatus.FAILED.value,
        },
    )


@router.subscriber(
    stream=StreamSub("jeanclode.events.manual_dispatch", group="jeanclode", consumer="worker-1")
)
async def consume_manual_dispatch(event: ManualDispatchEvent) -> None:
    """Handle a manual dispatch event — launch a container for a single issue."""
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    issue_id = uuid.UUID(event.issue_id)
    execution_id = uuid.UUID(event.execution_id)
    org_id = uuid.UUID(event.organization_id)

    def _load_dispatch_target(db: Session) -> tuple[Issue, Organization, str] | None:
        """Load the issue and its org for the launcher, detached but complete.

        ``None`` means the failure is already recorded on the execution and
        the caller only has to broadcast it.
        """
        issue = (
            db.query(Issue)
            .options(
                joinedload(Issue.repository)
                .joinedload(Repository.mapping)
                .joinedload(RepositoryMapping.mapped_repo),
            )
            .filter(Issue.id == issue_id)
            .first()
        )
        if not issue:
            logger.error(f"Manual dispatch: issue {issue_id} not found")
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="issue_not_found",
                error_detail=f"Issue {issue_id} not found",
            )
            return None

        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Manual dispatch: organization {org_id} not found")
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="org_not_found",
                error_detail=f"Organization {org_id} not found",
            )
            return None

        workspace_id = str(org.workspace_id) if org.workspace_id else ""
        # The launcher reads the repository/mapping chain eager-loaded above.
        # Expunging hands it fully-populated instances so the connection can
        # go back to the pool before the container launch, which is the slow
        # part of this handler by orders of magnitude.
        db.expunge_all()
        return issue, org, workspace_id

    target = await db_plugin.run_in_session(_load_dispatch_target)
    if target is None:
        await _publish_failure(execution_id, issue_id, org_id)
        return
    issue, org, workspace_id = target

    memory_workspace_id = await resolve_memory_workspace_id(org_id)
    container_id = await launch_container(
        [issue], org, execution_id, workspace_id=memory_workspace_id
    )

    # Publish SSE with result
    if container_id:
        await publish_execution_event(
            workspace_id=workspace_id,
            action="updated",
            payload={
                "execution_id": str(execution_id),
                "issue_id": str(issue_id),
                "status": ExecutionStatus.RUNNING.value,
                "container_id": container_id,
            },
        )
    else:
        await publish_execution_event(
            workspace_id=workspace_id,
            action="updated",
            payload={
                "execution_id": str(execution_id),
                "issue_id": str(issue_id),
                "status": ExecutionStatus.FAILED.value,
            },
        )


# ── Execution status consumer (from container watcher) ────────────


def _resolve_execution_targets(
    db_plugin: Any, execution_id: uuid.UUID
) -> tuple[str, list[uuid.UUID]]:
    """Resolve the workspace and linked issue IDs for an execution.

    Returns ``(workspace_id, issue_ids)``. ``workspace_id`` is empty when the
    execution has no linked issues, no repository, or no parent org workspace.
    """
    with db_plugin.session() as db:
        execution = (
            db.query(Execution)
            .options(
                selectinload(Execution.issues).joinedload(Issue.repository),
            )
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution or not execution.issues:
            return "", []
        issue_ids = [issue.id for issue in execution.issues]
        first = execution.issues[0]
        if not first.repository or not first.repository.org_id:
            return "", issue_ids
        org = db_get_org_by_id(db, first.repository.org_id)
        workspace_id = str(org.workspace_id) if org and org.workspace_id else ""
        return workspace_id, issue_ids


def _persist_triage_results(db: Session, execution: Execution, result: dict[str, Any]) -> None:
    """Persist per-issue triage decisions from the CLI's structured result payload.

    ``result["data"]["triages"]`` is keyed by the source issue's external_id
    (Sentry issue ID) and is only present for FIX-workflow executions — a
    missing/empty dict is a no-op for review/summary executions.
    """
    triages = result.get("data", {}).get("triages", {})
    if not triages:
        return

    for issue in execution.issues:
        triage_entry = triages.get(issue.external_id)
        triage = triage_entry.get("triage") if triage_entry else None
        if not triage:
            continue

        triage_result = (
            TriageResult.ACTIONABLE if triage.get("actionable") else TriageResult.NOT_ACTIONABLE
        )
        db_update_issue(
            db,
            issue.id,
            triage_result=triage_result.value,
            triage_metadata=triage,
        )


def _persist_pull_requests(db: Session, execution_id: uuid.UUID, result: dict[str, Any]) -> None:
    """Persist + link the fix PRs the CLI reported (Part D3).

    ``result["data"]["results"]`` is one entry per synthesis group; each has
    ``group.issue_ids`` (Sentry external ids) and ``repos`` keyed by repo
    name with ``{pr_url, pr_number, provider, ...}``. The git ``Repository``
    each PR belongs to is resolved from the group's mapped repo plus its repo
    group, matched to ``pr_url`` by ``web_url`` prefix — no branch names, and
    the ``execution_pull_requests`` link is the sole "bot opened this fix PR"
    marker the merge gate and dashboard read. ``issue_pull_requests`` is
    linked per group too, since a batch can split into several PRs and only
    the group's own issues are actually addressed by its PR(s).
    """
    results = (result or {}).get("data", {}).get("results", [])
    if not results:
        return

    execution = (
        db.query(Execution)
        .options(
            selectinload(Execution.issues)
            .joinedload(Issue.repository)
            .joinedload(Repository.mapping)
            .joinedload(RepositoryMapping.mapped_repo),
        )
        .filter(Execution.id == execution_id)
        .first()
    )
    if not execution or not execution.issues:
        return

    issues_by_external = {i.external_id: i for i in execution.issues}
    pr_ids: list[uuid.UUID] = []

    for group_result in results:
        group = group_result.get("group", {}) or {}
        repos = group_result.get("repos", {}) or {}
        if not repos:
            continue

        group_issue_ids = [
            issue.id
            for external_id in group.get("issue_ids", [])
            if (issue := issues_by_external.get(external_id)) is not None
        ]

        primary = None
        for external_id in group.get("issue_ids", []):
            issue = issues_by_external.get(external_id)
            if issue and issue.repository and issue.repository.mapped_repo:
                primary = issue.repository.mapped_repo
                break
        if primary is None:
            logger.warning(
                "persist fix PRs: no mapped repo for group %s (execution %s)",
                group.get("issue_ids"),
                execution_id,
            )
            continue

        candidates = [primary, *db_get_related_repos(db, primary.id)]
        by_url = {normalize_web_url(r.web_url): r for r in candidates if r.web_url}
        group_pr_ids: list[uuid.UUID] = []

        for info in repos.values():
            pr_url = info.get("pr_url")
            pr_number = info.get("pr_number")
            if not pr_url or not pr_number:
                logger.warning(
                    "persist fix PRs: entry missing pr_url/pr_number (execution %s): %s",
                    execution_id,
                    info,
                )
                continue
            normalized = normalize_web_url(pr_url)
            target = next(
                (
                    repo
                    for prefix, repo in by_url.items()
                    if normalized == prefix or normalized.startswith(prefix + "/")
                ),
                primary if len(candidates) == 1 else None,
            )
            if target is None:
                logger.warning(
                    "persist fix PRs: could not match %s to a repo (execution %s)",
                    pr_url,
                    execution_id,
                )
                continue

            # If the PR webhook already landed, that row carries the real
            # title/branches — link it, don't overwrite with placeholders.
            # Otherwise create a stub the next webhook reconciles in place.
            existing = db_get_pull_request(db, target.id, int(pr_number))
            if existing is not None:
                pr_ids.append(existing.id)
                group_pr_ids.append(existing.id)
                continue
            pr, _ = db_upsert_pull_request(
                db,
                repository_id=target.id,
                pr_number=int(pr_number),
                title=execution.issues[0].title or "Automated fix",
                author="jeanclode-bot",
                state=PRState.OPEN,
                pr_url=pr_url,
                head_branch=str(info.get("head_branch", "")),
                base_branch="",
            )
            pr_ids.append(pr.id)
            group_pr_ids.append(pr.id)

        for pr_id in group_pr_ids:
            db_link_issue_pull_requests(db, pr_id, group_issue_ids)

    if pr_ids:
        db_link_execution_pull_requests(db, execution_id, pr_ids)


@router.subscriber(
    stream=StreamSub(
        "jeanclode.sentry.execution.status",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def consume_execution_status(message: ExecutionStatusMessage) -> None:
    """Handle execution status updates from the sentry container watcher.

    Stream is scoped to sentry plugin — only sentry containers publish here.
    Consumer group ensures exactly-once processing across pods.
    Updates DB + publishes SSE. Gated so a reconcile burst can't open more
    DB sessions than the pool holds.
    """
    async with status_consumer_gate:
        await _handle_execution_status(message)


async def _handle_execution_status(message: ExecutionStatusMessage) -> None:
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    try:
        execution_id = uuid.UUID(message.execution_id)

        with db_plugin.session() as db:
            execution = db_update_execution_status(
                db,
                execution_id,
                message.status,
                error_type=message.error_type,
                error_detail=message.error_message,
            )
            if execution and message.result:
                _persist_triage_results(db, execution, message.result)
                _persist_pull_requests(db, execution_id, message.result)

            # Read fields while the session is still open — a nested commit
            # in _persist_triage_results (via db_update_issue) expires every
            # object in the session, including `execution`, so attribute
            # access after the `with` block closes raises DetachedInstanceError.
            if execution:
                execution_status = execution.status
                execution_error_type = execution.error_type
                execution_error_detail = execution.error_detail

        if execution:
            logger.info(
                "Execution %s → %s (plugin=sentry)",
                str(execution_id)[:8],
                message.status,
            )

            workspace_id, issue_ids = _resolve_execution_targets(db_plugin, execution_id)
            base_payload: dict[str, Any] = {
                "execution_id": str(execution_id),
                "status": execution_status,
                "container_id": message.container_id,
                "error_type": execution_error_type,
                "error_detail": execution_error_detail,
            }
            # One event per change, not per linked issue: every event makes
            # every open dashboard refetch.
            await publish_execution_event(
                workspace_id=workspace_id,
                action="updated",
                payload={
                    **base_payload,
                    "issue_id": str(issue_ids[0]) if issue_ids else None,
                    "issue_ids": [str(issue_id) for issue_id in issue_ids],
                },
            )
        else:
            logger.warning(
                "Execution %s not found for status update",
                str(execution_id),
            )

    except Exception as e:
        logger.exception(
            "Failed to process execution status update",
            extra={
                "execution_id": message.execution_id,
                "status": message.status,
                "error": str(e),
            },
        )
        raise
