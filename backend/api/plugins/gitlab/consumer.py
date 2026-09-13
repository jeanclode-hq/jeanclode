"""FastStream consumers for the gitlab plugin.

Subscribers:
- ``jeanclode.events.gitlab.manual_dispatch`` — manual + webhook-driven
  workflow trigger (REVIEW / SUMMARY / RESPOND / ISSUE_RESOLVE)
- ``jeanclode.gitlab.execution.status`` — container watcher status updates
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from faststream.redis import RedisRouter, StreamSub
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload, selectinload

from api.context import get_current_app
from api.database.execution import db_mark_execution_scheduled, db_update_execution_status
from api.database.issue import db_get_issue_by_id
from api.database.organization import db_get_org_by_id
from api.database.repository import db_get_repository_by_id
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.pull_requests import PullRequest
from api.plugins.container.consumer_gate import status_consumer_gate
from api.plugins.container.dispatch_inputs import resolve_memory_workspace_id
from api.plugins.container.respond_queue import dispatch_next_queued_respond
from api.plugins.container.retry_dispatch import (
    recheck_issue_still_actionable,
    recheck_pull_request_still_actionable,
    resolve_retry_context,
)
from api.plugins.container.status_comment import (
    sync_status_comment_for_issue,
    sync_status_comment_for_pull_request,
)
from api.plugins.gitlab.launch import (
    launch_container,
    launch_issue_resolve_container,
    launch_respond_container,
)
from api.sse.publishers import publish_pull_request_event

logger = logging.getLogger(__name__)

router = RedisRouter()


class ManualDispatchEvent(BaseModel):
    """Event published when a workflow is triggered (manual or webhook).

    REVIEW/SUMMARY require ``pull_request_id``. RESPOND uses either
    ``pull_request_id`` (mention on an MR) or ``issue_id`` (mention on a
    standalone issue) and carries the comment ``target_url`` — the CLI
    fetches the body and surface metadata from the GitLab API itself.

    ``organization_id``/``workflow`` are optional because the
    scheduled-redispatch poller (ADR-010) only ever pushes
    ``{"execution_id": ...}`` — a retry poke, not a full dispatch payload.
    ``consume_manual_dispatch`` reconstructs everything else from the
    ``Execution`` row itself when they're absent.
    """

    execution_id: str
    organization_id: str | None = None
    workflow: str | None = None
    pull_request_id: str | None = None
    issue_id: str | None = None
    target_url: str | None = None


class ExecutionStatusMessage(BaseModel):
    """Status update from the container watcher."""

    execution_id: str
    status: str
    container_id: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    error_type: str | None = None
    result: dict[str, Any] | None = None
    # Set only when status == "scheduled" (ADR-010) — every LLM credential
    # was found stale mid-run.
    retry_at: str | None = None
    timestamp: str | None = None


async def _publish_failure(
    execution_id: uuid.UUID,
    pr_id: uuid.UUID,
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
    await publish_pull_request_event(
        workspace_id=workspace_id,
        action="updated",
        payload={
            "execution_id": str(execution_id),
            "pull_request_id": str(pr_id),
            "status": ExecutionStatus.FAILED.value,
        },
    )


async def _consume_respond_dispatch(
    event: ManualDispatchEvent,
    execution_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    """RESPOND branch — symmetric to the GitHub plugin's handler."""
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin or not event.target_url:
        logger.error("Respond dispatch missing db plugin or target_url")
        return

    repo = None

    with db_plugin.session() as db:
        if event.pull_request_id:
            pr_id_obj = uuid.UUID(event.pull_request_id)
            pr = (
                db.query(PullRequest)
                .options(joinedload(PullRequest.repository))
                .filter(PullRequest.id == pr_id_obj)
                .first()
            )
            if not pr:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="pull_request_not_found",
                    error_detail=f"MR {pr_id_obj} not found",
                )
                return
            repo = pr.repository
            if repo is not None:
                db.expunge(repo)
        elif event.issue_id:
            issue = db_get_issue_by_id(db, uuid.UUID(event.issue_id))
            if not issue:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="issue_not_found",
                    error_detail=f"Issue {event.issue_id} not found",
                )
                return
            repo = db_get_repository_by_id(db, issue.repository_id)
            if repo is not None:
                db.expunge(repo)
        else:
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="no_target",
                error_detail="Respond dispatch missing both pull_request_id and issue_id",
            )
            return

        if not db_get_org_by_id(db, org_id):
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="org_not_found",
                error_detail=f"Organization {org_id} not found",
            )
            return

    # RESPOND runs in the background; nothing in the UI is gated on its
    # status, so we skip SSE publishing here. Only REVIEW broadcasts.
    memory_workspace_id = await resolve_memory_workspace_id(org_id)
    await launch_respond_container(
        target_url=event.target_url,
        org_id=org_id,
        repo=repo,
        execution_id=execution_id,
        workspace_id=memory_workspace_id,
    )
    if event.pull_request_id:
        await sync_status_comment_for_pull_request(uuid.UUID(event.pull_request_id))
    elif event.issue_id:
        await sync_status_comment_for_issue(uuid.UUID(event.issue_id))


async def _consume_issue_resolve_dispatch(
    event: ManualDispatchEvent,
    execution_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    """Branch of ``consume_manual_dispatch`` for ISSUE_RESOLVE."""
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin or not event.issue_id:
        logger.error("Issue-resolve dispatch missing db plugin or issue_id")
        return

    with db_plugin.session() as db:
        issue = db_get_issue_by_id(db, uuid.UUID(event.issue_id))
        if not issue:
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="issue_not_found",
                error_detail=f"Issue {event.issue_id} not found",
            )
            return

        if not issue.issue_url:
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="missing_issue_url",
                error_detail=f"Issue {event.issue_id} has no issue_url",
            )
            return

        issue_url = issue.issue_url
        repo = db_get_repository_by_id(db, issue.repository_id)
        if repo is not None:
            db.expunge(repo)

        if not db_get_org_by_id(db, org_id):
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="org_not_found",
                error_detail=f"Organization {org_id} not found",
            )
            return

    memory_workspace_id = await resolve_memory_workspace_id(org_id)
    await launch_issue_resolve_container(
        issue_url=issue_url,
        org_id=org_id,
        repo=repo,
        execution_id=execution_id,
        workspace_id=memory_workspace_id,
    )
    await sync_status_comment_for_issue(uuid.UUID(event.issue_id))


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.manual_dispatch",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def consume_manual_dispatch(event: ManualDispatchEvent) -> None:
    """Handle a manual or webhook-driven workflow dispatch.

    A retry poke from the scheduled-redispatch poller (ADR-010) carries
    only ``execution_id`` — ``organization_id`` is absent. In that case the
    rest of the dispatch context is reconstructed from the ``Execution``
    row (and its ``retry_target_url`` for RESPOND) before falling through
    to the exact same logic a fresh webhook dispatch already goes through.
    """
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    execution_id = uuid.UUID(event.execution_id)

    if event.organization_id is None:
        with db_plugin.session() as db:
            retry_context = resolve_retry_context(db, execution_id)
            if retry_context is None or retry_context.organization_id is None:
                logger.error("Retry dispatch: execution %s has no resolvable target", execution_id)
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="execution_not_found",
                    error_detail=f"Execution {execution_id} has no resolvable retry target",
                )
                return

            if retry_context.pull_request_id and not recheck_pull_request_still_actionable(
                db,
                execution_id=execution_id,
                pull_request_id=uuid.UUID(retry_context.pull_request_id),
            ):
                return
            if retry_context.issue_id and not recheck_issue_still_actionable(
                db, execution_id=execution_id, issue_id=uuid.UUID(retry_context.issue_id)
            ):
                return

        event = ManualDispatchEvent(
            execution_id=event.execution_id,
            organization_id=retry_context.organization_id,
            workflow=retry_context.workflow,
            pull_request_id=retry_context.pull_request_id,
            issue_id=retry_context.issue_id,
            target_url=retry_context.target_url,
        )

    org_id = uuid.UUID(event.organization_id)
    try:
        workflow = ExecutionWorkflow(event.workflow or "")
    except ValueError:
        logger.error("Manual dispatch: unknown workflow %s", event.workflow)
        with db_plugin.session() as db:
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="unknown_workflow",
                error_detail=f"Unknown workflow: {event.workflow}",
            )
        return

    if workflow == ExecutionWorkflow.RESPOND:
        await _consume_respond_dispatch(event, execution_id, org_id)
        return

    if workflow == ExecutionWorkflow.ISSUE_RESOLVE:
        await _consume_issue_resolve_dispatch(event, execution_id, org_id)
        return

    if not event.pull_request_id:
        logger.error("Manual dispatch: missing pull_request_id for workflow %s", workflow)
        with db_plugin.session() as db:
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="missing_pull_request_id",
                error_detail="pull_request_id required for review/summary",
            )
        return

    pr_id = uuid.UUID(event.pull_request_id)

    def _load_dispatch_target(db: Session) -> tuple[PullRequest, str] | None:
        """Load the MR and its org, expunged for use once the session is gone.

        ``None`` means the failure is already recorded on the execution and
        the caller only has to broadcast it.
        """
        pr = (
            db.query(PullRequest)
            .options(joinedload(PullRequest.repository))
            .filter(PullRequest.id == pr_id)
            .first()
        )
        if not pr:
            logger.error("Manual dispatch: MR %s not found", pr_id)
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="pull_request_not_found",
                error_detail=f"MR {pr_id} not found",
            )
            return None

        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error("Manual dispatch: organization %s not found", org_id)
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="org_not_found",
                error_detail=f"Organization {org_id} not found",
            )
            return None

        workspace_id = str(org.workspace_id) if org.workspace_id else ""
        if pr.repository is not None:
            db.expunge(pr.repository)
        db.expunge(pr)
        return pr, workspace_id

    target = await db_plugin.run_in_session(_load_dispatch_target)
    if target is None:
        if workflow == ExecutionWorkflow.REVIEW:
            await _publish_failure(execution_id, pr_id, org_id)
        return
    pr, workspace_id = target

    memory_workspace_id = await resolve_memory_workspace_id(org_id)
    container_id = await launch_container(
        workflow, pr, execution_id, workspace_id=memory_workspace_id
    )
    await sync_status_comment_for_pull_request(pr_id)

    # Only REVIEW broadcasts status to the frontend.
    if workflow != ExecutionWorkflow.REVIEW:
        return
    status = ExecutionStatus.RUNNING.value if container_id else ExecutionStatus.FAILED.value
    await publish_pull_request_event(
        workspace_id=workspace_id,
        action="updated",
        payload={
            "execution_id": str(execution_id),
            "pull_request_id": str(pr_id),
            "status": status,
            "container_id": container_id,
            "workflow": workflow.value,
        },
    )


def _resolve_execution_targets(
    db_plugin: Any, execution_id: uuid.UUID
) -> tuple[str, list[uuid.UUID]]:
    """Resolve the workspace and linked PR IDs for an execution."""
    with db_plugin.session() as db:
        execution = (
            db.query(Execution)
            .options(
                selectinload(Execution.pull_requests).joinedload(PullRequest.repository),
            )
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution or not execution.pull_requests:
            return "", []
        pr_ids = [pr.id for pr in execution.pull_requests]
        first = execution.pull_requests[0]
        if not first.repository or not first.repository.org_id:
            return "", pr_ids
        org = db_get_org_by_id(db, first.repository.org_id)
        workspace_id = str(org.workspace_id) if org and org.workspace_id else ""
        return workspace_id, pr_ids


async def _sync_status_comments_for_execution(db_plugin: Any, execution_id: uuid.UUID) -> None:
    """Upsert the sticky status comment on every PR/issue this execution targets."""
    with db_plugin.session() as db:
        execution = (
            db.query(Execution)
            .options(selectinload(Execution.pull_requests), selectinload(Execution.issues))
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution:
            return
        pr_ids = [pr.id for pr in execution.pull_requests]
        issue_ids = [issue.id for issue in execution.issues]

    for pr_id in pr_ids:
        await sync_status_comment_for_pull_request(pr_id)
    for issue_id in issue_ids:
        await sync_status_comment_for_issue(issue_id)


@router.subscriber(
    stream=StreamSub(
        "jeanclode.gitlab.execution.status",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def consume_execution_status(message: ExecutionStatusMessage) -> None:
    """Handle execution status updates from the gitlab container watcher.

    Gated so a reconcile burst can't open more DB sessions than the pool holds.
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
            if message.status == ExecutionStatus.SCHEDULED.value and message.retry_at:
                # A mid-run rate-limit hit (ADR-010) — the watcher already
                # marked the credential stale; this schedules the redispatch
                # poller to pick the execution back up once it's due.
                execution = db_mark_execution_scheduled(
                    db, execution_id, retry_at=datetime.fromisoformat(message.retry_at)
                )
            else:
                execution = db_update_execution_status(
                    db,
                    execution_id,
                    message.status,
                    error_type=message.error_type,
                    error_detail=message.error_message,
                )

            # Read fields while the session is still open — attribute access
            # after the `with` block closes can raise DetachedInstanceError
            # if anything expires the session's objects (e.g. a later commit
            # on the same session).
            if execution:
                execution_status = execution.status
                execution_error_type = execution.error_type
                execution_error_detail = execution.error_detail
                execution_workflow = execution.workflow

        if execution:
            logger.info(
                "Execution %s → %s (plugin=gitlab)",
                str(execution_id)[:8],
                message.status,
            )

            await _sync_status_comments_for_execution(db_plugin, execution_id)

            if execution_workflow == ExecutionWorkflow.RESPOND.value and execution_status in (
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FAILED.value,
            ):
                await dispatch_next_queued_respond(execution_id)

            # Only REVIEW broadcasts status — UI watches review progress
            # on PR rows; SUMMARY and RESPOND run silently.
            if execution_workflow != ExecutionWorkflow.REVIEW.value:
                return

            workspace_id, pr_ids = _resolve_execution_targets(db_plugin, execution_id)
            base_payload: dict[str, Any] = {
                "execution_id": str(execution_id),
                "status": execution_status,
                "container_id": message.container_id,
                "error_type": execution_error_type,
                "error_detail": execution_error_detail,
                "workflow": execution_workflow,
            }
            if pr_ids:
                # One event per change, not per linked MR.
                await publish_pull_request_event(
                    workspace_id=workspace_id,
                    action="updated",
                    payload={
                        **base_payload,
                        "pull_request_id": str(pr_ids[0]),
                        "pull_request_ids": [str(pr_id) for pr_id in pr_ids],
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


# ── Issue-resolve consumer ─────────────────────────────────────────


class IssueResolveEvent(BaseModel):
    """Event published when an issue-resolve workflow is triggered.

    ``issue_id``/``organization_id``/``workflow`` are optional for the same
    retry-poke reason as :class:`ManualDispatchEvent` — see there.
    """

    execution_id: str
    issue_id: str | None = None
    organization_id: str | None = None
    workflow: str | None = None


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.issue_resolve",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def consume_issue_resolve(event: IssueResolveEvent) -> None:
    """Handle a webhook-driven issue-resolve dispatch for GitLab.

    Looks up the issue URL from the DB, then launches a sandboxed
    container with ``issue-resolve <issue_url>``. A retry poke from the
    scheduled-redispatch poller (ADR-010) carries only ``execution_id`` —
    the rest is reconstructed from the ``Execution`` row.
    """
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    execution_id = uuid.UUID(event.execution_id)

    if event.organization_id is None:
        with db_plugin.session() as db:
            retry_context = resolve_retry_context(db, execution_id)
            if (
                retry_context is None
                or retry_context.organization_id is None
                or retry_context.issue_id is None
            ):
                logger.error("Retry dispatch: execution %s has no resolvable target", execution_id)
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="execution_not_found",
                    error_detail=f"Execution {execution_id} has no resolvable retry target",
                )
                return
            if not recheck_issue_still_actionable(
                db, execution_id=execution_id, issue_id=uuid.UUID(retry_context.issue_id)
            ):
                return

        event = IssueResolveEvent(
            execution_id=event.execution_id,
            issue_id=retry_context.issue_id,
            organization_id=retry_context.organization_id,
            workflow=retry_context.workflow,
        )

    org_id = uuid.UUID(event.organization_id)

    with db_plugin.session() as db:
        issue = db_get_issue_by_id(db, uuid.UUID(event.issue_id))
        if not issue:
            logger.error("Issue-resolve: issue %s not found", event.issue_id)
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="issue_not_found",
                error_detail=f"Issue {event.issue_id} not found",
            )
            return

        if not issue.issue_url:
            logger.error("Issue-resolve: issue %s has no issue_url", event.issue_id)
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="missing_issue_url",
                error_detail=f"Issue {event.issue_id} has no issue_url",
            )
            return

        issue_url = issue.issue_url
        repo = db_get_repository_by_id(db, issue.repository_id)
        if repo is not None:
            db.expunge(repo)

        if not db_get_org_by_id(db, org_id):
            logger.error("Issue-resolve: organization %s not found", org_id)
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="org_not_found",
                error_detail=f"Organization {org_id} not found",
            )
            return

    memory_workspace_id = await resolve_memory_workspace_id(org_id)
    await launch_issue_resolve_container(
        issue_url=issue_url,
        org_id=org_id,
        repo=repo,
        execution_id=execution_id,
        workspace_id=memory_workspace_id,
    )
    await sync_status_comment_for_issue(uuid.UUID(event.issue_id))
