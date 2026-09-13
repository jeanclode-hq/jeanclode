"""Executions endpoints — cancellation of SCHEDULED and RUNNING executions."""

import logging
import uuid
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from api.context import get_current_app
from api.database import run_in_session
from api.database.execution import db_cancel_running_execution, db_cancel_scheduled_execution
from api.database.organization import db_get_org_by_id
from api.models import User
from api.models.executions import Execution, ExecutionStatus
from api.models.issues import Issue
from api.models.pull_requests import PullRequest
from api.plugins.container.backend import ContainerBackend
from api.plugins.container.respond_queue import dispatch_next_queued_respond
from api.routers.auth.dependencies import get_current_user, verify_workspace_access_from_path
from api.sse.publishers import publish_execution_event, publish_pull_request_event

from .schemas import CancelExecutionResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/executions", tags=["Executions"])


class _LoadedExecution(NamedTuple):
    """What the cancel handler needs, read while the session was open."""

    status: str
    provider: str
    container_id: str | None
    workspace_id: uuid.UUID
    pull_request_ids: list[uuid.UUID]
    issue_ids: list[uuid.UUID]


def _resolve_execution_workspace_id(db: Session, execution: Execution) -> uuid.UUID | None:
    """Resolve the workspace that owns an execution's target repository.

    An execution links either pull requests or issues (never both) — see
    :func:`api.database.execution.db_create_execution`.
    """
    if execution.pull_requests:
        repo = execution.pull_requests[0].repository
    elif execution.issues:
        repo = execution.issues[0].repository
    else:
        return None
    if not repo:
        return None
    org = db_get_org_by_id(db, repo.org_id)
    return org.workspace_id if org else None


def _resolve_container_backend(provider: str) -> ContainerBackend | None:
    """Get the live ContainerBackend for a provider's watcher, if any.

    There's no separate registry — the watcher instance each plugin creates
    in ``watch()`` at startup is the one live backend per plugin, and
    ``launch_container`` for every provider reaches it the same way
    (``<plugin>._watcher.backend``).
    """
    app = get_current_app()
    plugin = {"github": app.github, "gitlab": app.gitlab, "sentry": app.sentry}.get(provider)
    watcher = getattr(plugin, "_watcher", None)
    return watcher.backend if watcher else None


@router.post(
    "/{execution_id}/cancel",
    operation_id="cancel_execution",
    response_model=CancelExecutionResponse,
)
async def cancel_execution(
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> CancelExecutionResponse:
    """Cancel an execution — SCHEDULED (ADR-010), or RUNNING.

    SCHEDULED: nothing has been dispatched yet, so this is a single
    conditional status update, no container involved.

    RUNNING: the DB row is cancelled the same conditional way, then the
    actual container/Job is killed best-effort via
    ``ContainerBackend.stop_container`` and unregistered from the watcher's
    active-execution set — so a completion the container reports in the
    meantime is ignored downstream rather than clobbering the cancellation.

    QUEUED is deliberately excluded, not just not-yet-supported: it has no
    container yet, and dispatch consuming its own event off the stream can
    lag admission by up to the redispatch/poll interval. Cancelling during
    that window would flip the row to CANCELLED while dispatch, unaware,
    goes on to launch the container anyway a moment later — orphaning it
    with nothing left watching to kill it. Cancel becomes possible the
    moment the row flips to RUNNING, once a container actually exists.

    Both conditional updates guard the same race: the redispatch poller (for
    SCHEDULED) or the watcher's own exit handling (for RUNNING) could reach
    a terminal state at the same instant — losing that race is reported as
    409, not silent success.
    """

    def _load(db: Session) -> _LoadedExecution:
        execution = (
            db.query(Execution)
            .options(
                selectinload(Execution.pull_requests).joinedload(PullRequest.repository),
                selectinload(Execution.issues).joinedload(Issue.repository),
            )
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution:
            raise HTTPException(status_code=404, detail="Execution not found")

        workspace_id = _resolve_execution_workspace_id(db, execution)
        if not workspace_id:
            raise HTTPException(status_code=404, detail="Workspace not found for execution")
        verify_workspace_access_from_path(db, current_user, workspace_id)

        return _LoadedExecution(
            status=execution.status,
            provider=execution.provider,
            container_id=execution.container_id,
            workspace_id=workspace_id,
            pull_request_ids=[pr.id for pr in execution.pull_requests],
            issue_ids=[issue.id for issue in execution.issues],
        )

    loaded = await run_in_session(_load)

    if loaded.status == ExecutionStatus.SCHEDULED.value:
        cancelled = await run_in_session(lambda db: db_cancel_scheduled_execution(db, execution_id))
    elif loaded.status == ExecutionStatus.RUNNING.value:
        cancelled = await run_in_session(lambda db: db_cancel_running_execution(db, execution_id))
        if cancelled:
            backend = _resolve_container_backend(loaded.provider)
            if backend:
                await backend.unregister_execution(str(execution_id))
                if loaded.container_id:
                    await backend.stop_container(loaded.container_id)
            else:
                logger.warning(
                    "No live container backend for provider %s — execution %s marked "
                    "cancelled but its container was not signalled to stop",
                    loaded.provider,
                    execution_id,
                )
    elif loaded.status == ExecutionStatus.QUEUED.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "Execution is still queued — cancellation isn't supported until it "
                "starts running, to avoid racing its dispatch"
            ),
        )
    else:
        raise HTTPException(
            status_code=409,
            detail=f"Execution is already {loaded.status} — nothing to cancel",
        )

    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=(
                "Execution is no longer cancellable — it may have already "
                "redispatched or reached a terminal state"
            ),
        )

    # A cancelled RESPOND execution never crosses the status stream (that's
    # the point — see the SCHEDULED/RUNNING branches above), so it's the one
    # terminal transition dispatch_next_queued_respond can't observe on its
    # own. A no-op for every other workflow.
    await dispatch_next_queued_respond(execution_id)

    # One event per cancellation, not per linked PR/issue: every event makes
    # every open dashboard refetch.
    base_payload = {"execution_id": str(execution_id), "status": ExecutionStatus.CANCELLED.value}
    if loaded.pull_request_ids:
        pr_ids = [str(pr_id) for pr_id in loaded.pull_request_ids]
        await publish_pull_request_event(
            workspace_id=str(loaded.workspace_id),
            action="updated",
            payload={**base_payload, "pull_request_id": pr_ids[0], "pull_request_ids": pr_ids},
        )
    else:
        issue_ids = [str(issue_id) for issue_id in loaded.issue_ids]
        await publish_execution_event(
            workspace_id=str(loaded.workspace_id),
            action="updated",
            payload={
                **base_payload,
                "issue_id": issue_ids[0] if issue_ids else None,
                "issue_ids": issue_ids,
            },
        )

    return CancelExecutionResponse(execution_id=str(execution_id), cancelled=True)
