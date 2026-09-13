"""Issues dashboard endpoints."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.database import (
    compute_display_status,
    db_get_issue_by_id,
    db_get_issue_detail,
    db_get_issues_paginated,
    db_list_issue_repositories,
    db_update_issue,
    get_session,
    run_in_session,
)
from api.database.execution import (
    db_create_execution,
    db_get_failed_execution_count,
    db_get_latest_execution_for_issue,
    db_has_active_execution,
)
from api.models import User
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.issues import TriageResult
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.settings import RepoSettings
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.plugins.sentry.consumer import ManualDispatchEvent
from api.routers.auth.dependencies import get_current_user, verify_workspace_access
from api.routers.base_schema import ListPaginatedResponse, PaginationMeta
from api.sse.publishers import publish_execution_event

from .schemas import (
    FixTriggerResponse,
    IssueDetailResponse,
    IssueResponse,
    RepoOption,
    RepoOptionPage,
)
from .utils import issue_to_detail, issue_to_response, verify_issue_workspace_access

router = APIRouter(prefix="/issues", tags=["Issues"])


class _QueuedDispatch(NamedTuple):
    """The ids a manual dispatch needs to publish, read while the session was open."""

    execution_id: uuid.UUID
    issue_id: uuid.UUID
    org_id: uuid.UUID
    provider: str
    workspace_id: str


@router.get(
    "",
    operation_id="list_issues",
    response_model=ListPaginatedResponse[IssueResponse],
)
def list_issues(
    workspace_id: uuid.UUID = Query(..., description="Workspace ID"),
    status: str | None = Query(None, description="Comma-separated status filter"),
    search: str | None = Query(None, description="Search by title"),
    source_org_id: uuid.UUID | None = Query(None, description="Filter by source org"),
    repository_id: uuid.UUID | None = Query(None, description="Filter by repository/project"),
    author: str | None = Query(None, description="Filter by author"),
    period: str | None = Query(None, description="Time period: today, 7days, 30days, 90days"),
    mapped_only: bool = Query(False, description="Only show issues whose repo has a git mapping"),
    execution_status: str | None = Query(
        None,
        description="Comma-separated execution status filter: pending, running, completed, failed",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(25, ge=5, le=200, description="Items per page"),
    current_user: User = Depends(verify_workspace_access),
    db: Session = Depends(get_session),
) -> ListPaginatedResponse[IssueResponse]:
    """List issues for a workspace with filters and pagination."""
    status_list = [s.strip() for s in status.split(",") if s.strip()] if status else None
    execution_status_list = (
        [s.strip() for s in execution_status.split(",") if s.strip()] if execution_status else None
    )

    _period_days = {"today": 1, "7days": 7, "30days": 30, "90days": 90}
    since: datetime | None = None
    if period and period in _period_days:
        since = datetime.now(UTC) - timedelta(days=_period_days[period])

    results, total = db_get_issues_paginated(
        db,
        workspace_id,
        page=page,
        limit=limit,
        status=status_list,
        search=search,
        source_org_id=source_org_id,
        repository_id=repository_id,
        author=author,
        since=since,
        mapped_only=mapped_only,
        execution_status=execution_status_list,
    )

    return ListPaginatedResponse[IssueResponse](
        objects=[
            issue_to_response(issue, computed_status, workflow, exec_status, exec_id)
            for issue, computed_status, workflow, exec_status, exec_id in results
        ],
        pagination=PaginationMeta(total=total, limit=limit, page=page),
    )


@router.get(
    "/repositories",
    operation_id="list_issue_repositories",
    response_model=RepoOptionPage,
)
def list_issue_repositories(
    workspace_id: uuid.UUID = Query(..., description="Workspace ID"),
    source_org_id: uuid.UUID | None = Query(None, description="Restrict to a single org"),
    search: str | None = Query(None, description="Case-insensitive name filter"),
    page: int = Query(1, ge=1, description="1-indexed page"),
    limit: int = Query(25, ge=1, le=200, description="Page size"),
    current_user: User = Depends(verify_workspace_access),
    db: Session = Depends(get_session),
) -> RepoOptionPage:
    """One page of distinct repositories/projects that have issues, for the repo filter dropdown.

    Paginated and searchable server-side so the dropdown stays responsive for
    workspaces with many repositories — callers page through until
    ``has_more`` is false.
    """
    repos, total = db_list_issue_repositories(
        db,
        workspace_id,
        source_org_id=source_org_id,
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


@router.get(
    "/{issue_id}",
    operation_id="get_issue",
    response_model=IssueDetailResponse,
)
def get_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> IssueDetailResponse:
    """Get issue detail."""
    verify_issue_workspace_access(db, issue_id, current_user)

    issue = db_get_issue_detail(db, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Compute display status for detail view
    latest_exec = db_get_latest_execution_for_issue(db, issue_id)
    exec_status = latest_exec.status if latest_exec else None
    pr_state = (
        latest_exec.pull_requests[0].state if latest_exec and latest_exec.pull_requests else None
    )
    computed = compute_display_status(exec_status, pr_state, issue.triage_result)

    return issue_to_detail(issue, computed, exec_status, latest_exec.id if latest_exec else None)


@router.post(
    "/{issue_id}/retry",
    operation_id="retry_issue",
    response_model=IssueResponse,
)
def retry_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> IssueResponse:
    """Retry a failed issue — sets triage_result back to actionable."""
    verify_issue_workspace_access(db, issue_id, current_user)

    issue = db_get_issue_by_id(db, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if db_has_active_execution(db, issue_id):
        raise HTTPException(status_code=409, detail="Issue has an active execution")

    failed_count = db_get_failed_execution_count(db, issue_id)
    if failed_count == 0 and issue.triage_result != TriageResult.NOT_ACTIONABLE:
        raise HTTPException(status_code=409, detail="Issue has no failed executions to retry")

    issue = db_update_issue(db, issue_id, triage_result=TriageResult.ACTIONABLE.value)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    detail = db_get_issue_detail(db, issue.id)
    if not detail:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue_to_response(detail, "pending", exec_status="pending")


@router.post(
    "/{issue_id}/dismiss",
    operation_id="dismiss_issue",
    response_model=IssueResponse,
)
def dismiss_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> IssueResponse:
    """Dismiss an issue by setting triage_result to NOT_ACTIONABLE."""
    verify_issue_workspace_access(db, issue_id, current_user)

    issue = db_update_issue(db, issue_id, triage_result=TriageResult.NOT_ACTIONABLE.value)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    detail = db_get_issue_detail(db, issue.id)
    if not detail:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue_to_response(detail, "not_actionable", exec_status="pending")


@router.post(
    "/{issue_id}/fix",
    operation_id="fix_issue",
    response_model=FixTriggerResponse,
)
async def fix_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> FixTriggerResponse:
    """Manually trigger a fix for an issue — launches a single CLI container."""

    def _queue(db: Session) -> _QueuedDispatch:
        verify_issue_workspace_access(db, issue_id, current_user)

        issue = db_get_issue_by_id(db, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        if db_has_active_execution(db, issue_id):
            raise HTTPException(status_code=409, detail="Issue already has an active execution")

        # Resolve issue → repository → organization
        repository = db.query(Repository).filter(Repository.id == issue.repository_id).first()
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        if not repository.mapped_repo_id:
            raise HTTPException(
                status_code=422,
                detail="Repository has no git mapping — configure it on the integrations page",
            )

        repo_settings = RepoSettings.model_validate(repository.settings or {})
        if not repo_settings.enabled:
            raise HTTPException(
                status_code=422,
                detail="Repository is disabled — enable it on the integrations settings",
            )

        org = db.query(Organization).filter(Organization.id == repository.org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Manual FIX dispatch is run by the sentry container plugin.
        execution = db_create_execution(
            db,
            provider="sentry",
            issues=[issue],
            workflow=ExecutionWorkflow.FIX.value,
            trigger=ExecutionTrigger.MANUAL.value,
            status=ExecutionStatus.QUEUED.value,
        )
        return _QueuedDispatch(
            execution_id=execution.id,
            issue_id=issue.id,
            org_id=org.id,
            provider=org.provider,
            workspace_id=str(org.workspace_id) if org.workspace_id else "",
        )

    queued = await run_in_session(_queue)

    # Publish to Redis stream for async processing
    broker = get_faststream_broker()
    event = ManualDispatchEvent(
        issue_id=str(queued.issue_id),
        execution_id=str(queued.execution_id),
        organization_id=str(queued.org_id),
    )
    await broker.publish(
        event.model_dump(),
        stream="jeanclode.events.manual_dispatch",
        maxlen=STREAM_MAXLEN,
    )

    # Publish SSE event for immediate frontend feedback
    await publish_execution_event(
        workspace_id=queued.workspace_id,
        action="created",
        payload={
            "execution_id": str(queued.execution_id),
            "issue_id": str(queued.issue_id),
            "status": ExecutionStatus.QUEUED.value,
            "trigger": ExecutionTrigger.MANUAL.value,
        },
    )

    return FixTriggerResponse(
        execution_id=queued.execution_id,
        status=ExecutionStatus.QUEUED.value,
    )


@router.post(
    "/{issue_id}/resolve",
    operation_id="resolve_issue",
    response_model=FixTriggerResponse,
)
async def resolve_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> FixTriggerResponse:
    """Manually trigger an issue-resolve run for a GitHub/GitLab issue."""

    def _queue(db: Session) -> _QueuedDispatch:
        verify_issue_workspace_access(db, issue_id, current_user)

        issue = db_get_issue_by_id(db, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        if db_has_active_execution(db, issue_id, workflow=ExecutionWorkflow.ISSUE_RESOLVE.value):
            raise HTTPException(status_code=409, detail="Issue already has an active execution")

        repository = db.query(Repository).filter(Repository.id == issue.repository_id).first()
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        repo_settings = RepoSettings.model_validate(repository.settings or {})
        if not repo_settings.enabled:
            raise HTTPException(
                status_code=422,
                detail="Repository is disabled — enable it on the integrations settings",
            )

        org = db.query(Organization).filter(Organization.id == repository.org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        if org.provider not in ("github", "gitlab"):
            raise HTTPException(
                status_code=422, detail="issue-resolve only supports GitHub/GitLab issues"
            )

        execution = db_create_execution(
            db,
            provider=org.provider,
            issues=[issue],
            workflow=ExecutionWorkflow.ISSUE_RESOLVE.value,
            trigger=ExecutionTrigger.MANUAL.value,
            status=ExecutionStatus.QUEUED.value,
        )
        return _QueuedDispatch(
            execution_id=execution.id,
            issue_id=issue.id,
            org_id=org.id,
            provider=org.provider,
            workspace_id=str(org.workspace_id) if org.workspace_id else "",
        )

    queued = await run_in_session(_queue)

    broker = get_faststream_broker()
    await broker.publish(
        {
            "execution_id": str(queued.execution_id),
            "organization_id": str(queued.org_id),
            "workflow": ExecutionWorkflow.ISSUE_RESOLVE.value,
            "issue_id": str(queued.issue_id),
        },
        stream=f"jeanclode.events.{queued.provider}.manual_dispatch",
        maxlen=STREAM_MAXLEN,
    )

    await publish_execution_event(
        workspace_id=queued.workspace_id,
        action="created",
        payload={
            "execution_id": str(queued.execution_id),
            "issue_id": str(queued.issue_id),
            "status": ExecutionStatus.QUEUED.value,
            "trigger": ExecutionTrigger.MANUAL.value,
        },
    )

    return FixTriggerResponse(
        execution_id=queued.execution_id,
        status=ExecutionStatus.QUEUED.value,
    )
