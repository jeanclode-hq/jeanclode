"""Utility functions for issues endpoints."""

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.database import db_get_issue_by_id
from api.models import User
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.settings import RepoSettings
from api.models.workspaces import WorkspaceMembership

from .schemas import ExecutionSummary, IssueDetailResponse, IssueResponse, PullRequestSummary


def issue_to_response(
    issue,
    computed_status: str = "pending",
    workflow: str | None = None,
    exec_status: str | None = None,
    exec_id: uuid.UUID | None = None,
) -> IssueResponse:
    """Convert an Issue ORM object (with joined repository) to IssueResponse."""
    repo = issue.repository
    source = repo.organization.provider if repo.organization else repo.provider
    repo_settings = RepoSettings.model_validate(repo.settings or {})
    return IssueResponse(
        id=issue.id,
        external_id=issue.external_id,
        title=issue.title,
        culprit=issue.culprit,
        level=issue.level,
        status=issue.status,
        execution_status=exec_status or "pending",
        execution_id=exec_id,
        result=computed_status,
        event_count=issue.event_count,
        first_seen=issue.first_seen,
        last_seen=issue.last_seen,
        author=issue.author,
        source=source,
        source_org_id=repo.org_id,
        project=repo.name,
        issue_url=issue.issue_url,
        triage_result=issue.triage_result,
        workflow=workflow,
        has_mapping=source in ("github", "gitlab") or bool(repo.mapped_repo_id),
        repo_enabled=repo_settings.enabled,
        created_at=issue.created_at,
    )


def issue_to_detail(
    issue,
    computed_status: str = "pending",
    exec_status: str | None = None,
    exec_id: uuid.UUID | None = None,
) -> IssueDetailResponse:
    """Convert an Issue ORM object to IssueDetailResponse."""
    repo = issue.repository
    source = repo.organization.provider if repo.organization else repo.provider

    # Only PRs actually linked to this issue (via issue_pull_requests) — an
    # execution's own PR list can span other issues from the same batch.
    own_pr_ids = {pr.id for pr in issue.pull_requests} if hasattr(issue, "pull_requests") else set()

    executions = []
    if hasattr(issue, "executions") and issue.executions:
        for exc in sorted(issue.executions, key=lambda e: e.created_at, reverse=True):
            executions.append(
                ExecutionSummary(
                    id=exc.id,
                    workflow=exc.workflow,
                    trigger=exc.trigger,
                    status=exc.status,
                    container_id=exc.container_id,
                    error_type=exc.error_type,
                    error_detail=exc.error_detail,
                    created_at=exc.created_at,
                    pull_requests=[
                        PullRequestSummary(
                            id=pr.id,
                            pr_number=pr.pr_number,
                            title=pr.title,
                            state=pr.state,
                            pr_url=pr.pr_url,
                        )
                        for pr in exc.pull_requests
                        if pr.id in own_pr_ids
                    ],
                )
            )

    repo_settings = RepoSettings.model_validate(repo.settings or {})
    return IssueDetailResponse(
        id=issue.id,
        external_id=issue.external_id,
        title=issue.title,
        culprit=issue.culprit,
        level=issue.level,
        status=issue.status,
        execution_status=exec_status or "pending",
        execution_id=exec_id,
        result=computed_status,
        event_count=issue.event_count,
        first_seen=issue.first_seen,
        last_seen=issue.last_seen,
        author=issue.author,
        source=source,
        source_org_id=repo.org_id,
        project=repo.name,
        issue_url=issue.issue_url,
        triage_result=issue.triage_result,
        workflow=executions[0].workflow if executions else None,
        has_mapping=source in ("github", "gitlab") or bool(repo.mapped_repo_id),
        repo_enabled=repo_settings.enabled,
        created_at=issue.created_at,
        triage_metadata=issue.triage_metadata,
        executions=executions,
    )


def verify_issue_workspace_access(
    db: Session,
    issue_id: uuid.UUID,
    user: User,
) -> None:
    """Verify the user has workspace access to the issue's workspace.

    Resolves issue -> repository -> organization -> workspace -> membership.

    Raises:
        HTTPException: 404 if issue not found, 403 if no workspace access
    """
    issue = db_get_issue_by_id(db, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    repository = db.query(Repository).filter(Repository.id == issue.repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Issue not found")

    org = db.query(Organization).filter(Organization.id == repository.org_id).first()
    if not org or not org.workspace_id:
        raise HTTPException(status_code=404, detail="Issue not found")

    membership = (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == org.workspace_id,
            WorkspaceMembership.user_id == user.id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(status_code=403, detail="You don't have access to this workspace")
