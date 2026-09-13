"""Pull requests endpoints — list (workspace-scoped) and manual workflow trigger."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.database import (
    db_get_pull_request_by_id,
    db_get_workspace_pull_requests,
    db_has_active_pr_execution,
    db_list_pr_repositories,
    get_session,
    run_in_session,
)
from api.database.execution import db_create_execution, db_update_execution_status
from api.database.organization import db_get_org_by_id
from api.models import User
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.settings import RepoSettings
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.auth.dependencies import get_current_user, verify_workspace_access_from_path
from api.routers.base_schema import ListPaginatedResponse, PaginationMeta
from api.routers.issues.schemas import RepoOption, RepoOptionPage
from api.sse.publishers import publish_pull_request_event

from .schemas import PullRequestResponse, ReviewTriggerResponse
from .utils import row_to_pr_response

logger = logging.getLogger(__name__)


class _QueuedPrDispatch(NamedTuple):
    """What the publish needs, read while the session was open."""

    execution_id: uuid.UUID
    org_id: uuid.UUID
    provider: str
    workspace_id: str
    triggered_by: str


router = APIRouter(prefix="/workspaces/{workspace_id}/pull-requests", tags=["Pull Requests"])

# Sibling router for action endpoints — not workspace-scoped in the URL
# because the PR id is a workspace-unique surrogate; access control
# happens at the org/workspace level inside the handler.
actions_router = APIRouter(prefix="/pull-requests", tags=["Pull Requests"])


# Manual triggers are restricted to REVIEW. SUMMARY runs only via the
# webhook auto-trigger path — surfacing a button for it muddies the UX
# and matches the predecessor project's convention (one button per PR row).
_MANUAL_WORKFLOWS: dict[str, ExecutionWorkflow] = {
    "review": ExecutionWorkflow.REVIEW,
}


@router.get(
    "",
    operation_id="list_pull_requests",
    response_model=ListPaginatedResponse[PullRequestResponse],
)
def list_pull_requests(
    workspace_id: uuid.UUID,
    status: str | None = Query(None, description="Filter by PR state: open, merged, closed"),
    search: str | None = Query(None, description="Search by title"),
    org_id: uuid.UUID | None = Query(None, description="Filter by organization"),
    repository_id: uuid.UUID | None = Query(None, description="Filter by repository"),
    author: str | None = Query(None, description="Filter by author"),
    period: str | None = Query(None, description="Time period: today, 7days, 30days, 90days"),
    execution_status: str | None = Query(
        None,
        description="Comma-separated execution status filter: pending, running, completed, failed",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(25, ge=5, le=200, description="Items per page"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ListPaginatedResponse[PullRequestResponse]:
    """List pull requests for a workspace."""
    verify_workspace_access_from_path(db, current_user, workspace_id)
    execution_status_list = (
        [s.strip() for s in execution_status.split(",") if s.strip()] if execution_status else None
    )

    _period_days = {"today": 1, "7days": 7, "30days": 30, "90days": 90}
    since: datetime | None = None
    if period and period in _period_days:
        since = datetime.now(UTC) - timedelta(days=_period_days[period])

    results, total = db_get_workspace_pull_requests(
        db,
        workspace_id,
        page=page,
        limit=limit,
        status=status,
        search=search,
        org_id=org_id,
        repository_id=repository_id,
        author=author,
        since=since,
        execution_status=execution_status_list,
    )

    return ListPaginatedResponse[PullRequestResponse](
        objects=[row_to_pr_response(row) for row in results],
        pagination=PaginationMeta(total=total, limit=limit, page=page),
    )


@router.get(
    "/repositories",
    operation_id="list_pull_request_repositories",
    response_model=RepoOptionPage,
)
def list_pull_request_repositories(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID | None = Query(None, description="Restrict to a single org"),
    search: str | None = Query(None, description="Case-insensitive name filter"),
    page: int = Query(1, ge=1, description="1-indexed page"),
    limit: int = Query(25, ge=1, le=200, description="Page size"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RepoOptionPage:
    """One page of distinct repositories that have pull requests, for the repo filter dropdown.

    Paginated and searchable server-side, mirroring ``GET /issues/repositories``.
    """
    verify_workspace_access_from_path(db, current_user, workspace_id)
    repos, total = db_list_pr_repositories(
        db,
        workspace_id,
        org_id=org_id,
        search=search,
        page=page,
        limit=limit,
    )
    return RepoOptionPage(
        items=[
            RepoOption(id=repo.id, name=repo.name, org_name=org_name) for repo, org_name in repos
        ],
        total=total,
        page=page,
        has_more=(page * limit) < total,
    )


@actions_router.post(
    "/{pr_id}/{workflow}",
    operation_id="trigger_pull_request_workflow",
    response_model=ReviewTriggerResponse,
)
async def trigger_pr_workflow(
    pr_id: uuid.UUID,
    workflow: str,
    current_user: User = Depends(get_current_user),
) -> ReviewTriggerResponse:
    """Manually trigger a REVIEW or SUMMARY workflow on a PR.

    Only the PR's author may trigger manually — the gate compares the
    PR's stored ``author`` (the GitHub/GitLab login at PR creation time)
    against the current user's identity username for that provider.
    """
    workflow_enum = _MANUAL_WORKFLOWS.get(workflow)
    if workflow_enum is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported workflow '{workflow}' (allowed: review)",
        )

    def _queue(db: Session) -> _QueuedPrDispatch:
        pr = db_get_pull_request_by_id(db, pr_id)
        if not pr:
            raise HTTPException(status_code=404, detail="Pull request not found")

        repository = pr.repository
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_settings = RepoSettings.model_validate(repository.settings or {})
        if not repo_settings.enabled:
            raise HTTPException(
                status_code=422,
                detail="Repository is disabled — enable it on the integrations settings",
            )

        git_org = db_get_org_by_id(db, repository.org_id)
        if not git_org:
            raise HTTPException(status_code=404, detail="Organization not found")

        if not git_org.workspace_id:
            raise HTTPException(status_code=404, detail="Workspace not found")
        verify_workspace_access_from_path(db, current_user, git_org.workspace_id)

        provider = git_org.provider
        if provider not in ("github", "gitlab"):
            raise HTTPException(
                status_code=422,
                detail=f"Provider '{provider}' does not support PR workflows",
            )

        # Author gate — must match the current user's identity for this provider.
        identity = current_user.get_identity(provider)
        if not identity or identity.username != pr.author:
            raise HTTPException(
                status_code=403,
                detail="Only the PR author can manually trigger this workflow",
            )

        if db_has_active_pr_execution(db, pr_id, workflow=workflow_enum.value):
            raise HTTPException(
                status_code=409,
                detail=f"PR already has an active {workflow_enum.value} execution",
            )

        execution = db_create_execution(
            db,
            provider=provider,
            pull_requests=[pr],
            workflow=workflow_enum.value,
            trigger=ExecutionTrigger.MANUAL.value,
            status=ExecutionStatus.QUEUED.value,
        )
        return _QueuedPrDispatch(
            execution_id=execution.id,
            org_id=git_org.id,
            provider=provider,
            workspace_id=str(git_org.workspace_id),
            triggered_by=identity.username,
        )

    queued = await run_in_session(_queue)

    broker = get_faststream_broker()
    stream = f"jeanclode.events.{queued.provider}.manual_dispatch"
    try:
        await broker.publish(
            {
                "pull_request_id": str(pr_id),
                "execution_id": str(queued.execution_id),
                "organization_id": str(queued.org_id),
                "workflow": workflow_enum.value,
            },
            stream=stream,
            maxlen=STREAM_MAXLEN,
        )
    except Exception as exc:
        logger.exception(
            "Broker publish failed for execution %s — marking FAILED", queued.execution_id
        )
        await run_in_session(
            lambda db: db_update_execution_status(
                db,
                queued.execution_id,
                ExecutionStatus.FAILED.value,
                error_type="broker_publish_failed",
                error_detail="Failed to enqueue dispatch event",
            )
        )
        raise HTTPException(status_code=503, detail="Failed to enqueue workflow") from exc

    await publish_pull_request_event(
        workspace_id=queued.workspace_id,
        action="execution_created",
        payload={
            "pull_request_id": str(pr_id),
            "execution_id": str(queued.execution_id),
            "status": ExecutionStatus.QUEUED.value,
            "workflow": workflow_enum.value,
            "trigger": ExecutionTrigger.MANUAL.value,
        },
    )

    logger.info(
        "Manual %s queued for PR %s by %s",
        workflow_enum.value,
        pr_id,
        queued.triggered_by,
    )

    return ReviewTriggerResponse(
        execution_id=queued.execution_id,
        status=ExecutionStatus.QUEUED.value,
    )
