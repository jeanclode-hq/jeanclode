"""Workspace endpoints — list, create, stats, and finalize onboarding."""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.database import (
    db_ensure_org_membership,
    db_get_orgs_by_workspace,
    db_get_source_orgs,
    db_get_workspace_stats,
    get_session,
    run_in_session,
)
from api.database.dashboard import (
    db_get_active_repos,
    db_get_lgtm_leaderboard,
    db_get_top_pr_authors,
    db_get_workspace_active_executions,
    db_get_workspace_executions,
)
from api.database.workspace import (
    db_create_workspace,
    db_create_workspace_membership,
    db_get_workspace_by_slug,
    db_get_workspace_members,
    db_get_workspaces_by_user,
    db_is_workspace_member,
)
from api.models import User
from api.models.executions import Execution
from api.models.organizations import OrgMembership
from api.routers.auth.dependencies import get_current_user, verify_workspace_access_from_path
from api.sse.stats_cache import get_cached_stats, store_stats

from .schemas import (
    ActiveExecutionsResponse,
    ActiveRepoEntry,
    CreateWorkspaceRequest,
    LeaderboardsResponse,
    LgtmEntry,
    PrAuthorEntry,
    SourceSummary,
    WorkspaceExecutionResponse,
    WorkspaceExecutionsResponse,
    WorkspaceListResponse,
    WorkspaceMemberResponse,
    WorkspaceMembersResponse,
    WorkspaceResponse,
    WorkspaceSourcesResponse,
    WorkspaceStatsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])


def _slugify(name: str) -> str:
    """Convert a workspace name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


@router.get(
    "",
    operation_id="list_workspaces",
    response_model=WorkspaceListResponse,
)
def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkspaceListResponse:
    """List all workspaces the current user is a member of."""
    workspaces = db_get_workspaces_by_user(db, current_user.id)
    return WorkspaceListResponse(
        workspaces=[
            WorkspaceResponse(
                id=ws.id,
                name=ws.name,
                slug=ws.slug,
                created_at=ws.created_at,
                updated_at=ws.updated_at,
            )
            for ws in workspaces
        ]
    )


@router.post(
    "",
    operation_id="create_workspace",
    response_model=WorkspaceResponse,
    status_code=201,
)
def create_workspace(
    request: CreateWorkspaceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkspaceResponse:
    """Create a new workspace and add the current user as owner."""
    slug = _slugify(request.name)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid workspace name")

    existing = db_get_workspace_by_slug(db, slug)
    if existing:
        raise HTTPException(status_code=409, detail="A workspace with this name already exists")

    workspace = db_create_workspace(db, name=request.name, slug=slug)
    db_create_workspace_membership(db, workspace_id=workspace.id, user_id=current_user.id)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.post(
    "/{workspace_id}/finalize",
    operation_id="finalize_workspace_onboarding",
    status_code=200,
)
def finalize_workspace_onboarding(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    """Finalize onboarding — create memberships and queue membership sync.

    Called when the user clicks "Finish setup". Creates immediate owner
    memberships for the connecting user, then queues background tasks
    for membership sync.
    """
    if not db_is_workspace_member(db, workspace_id, current_user.id):
        raise HTTPException(status_code=403, detail="Not a member of this workspace")

    orgs = db_get_orgs_by_workspace(db, workspace_id)

    # Create immediate owner memberships for the connecting user
    for org in orgs:
        for identity in current_user.identities:
            existing = (
                db.query(OrgMembership)
                .filter(
                    OrgMembership.org_id == org.id,
                    OrgMembership.provider_identity_id == identity.id,
                )
                .first()
            )
            if not existing:
                db_ensure_org_membership(
                    db,
                    org_id=org.id,
                    provider_identity_id=identity.id,
                    role="owner",
                )

    db.commit()

    return {"message": "Onboarding finalized"}


@router.get(
    "/{workspace_id}/members",
    operation_id="list_workspace_members",
    response_model=WorkspaceMembersResponse,
)
def list_workspace_members(
    workspace_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkspaceMembersResponse:
    """List paginated workspace members."""
    verify_workspace_access_from_path(db, current_user, workspace_id)

    rows, total = db_get_workspace_members(db, workspace_id, page=page, limit=limit)

    members = []
    for membership, user in rows:
        providers = list(
            {i.provider for i in user.identities if i.provider in ("github", "gitlab")}
        )
        members.append(
            WorkspaceMemberResponse(
                user_id=user.id,
                username=user.display_username,
                avatar_url=user.avatar_url,
                providers=providers,
                joined_at=membership.created_at,
            )
        )

    return WorkspaceMembersResponse(
        members=members,
        total=total,
        page=page,
        has_more=(page * limit) < total,
    )


@router.get(
    "/{workspace_id}/sources",
    operation_id="get_workspace_sources",
    response_model=WorkspaceSourcesResponse,
)
def get_sources(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkspaceSourcesResponse:
    """Get connected sources for a workspace."""
    verify_workspace_access_from_path(db, current_user, workspace_id)

    # One entry per actual connection: the org a credential was attached to,
    # never the subgroups a GitLab group token happens to reach (see
    # db_get_source_orgs) — a tenant who connected one group should see that
    # one group here, not its whole namespace tree.
    orgs = db_get_source_orgs(db, workspace_id)

    sources = [
        SourceSummary(
            id=org.id,
            name=org.name,
            provider=org.provider,
            avatar_url=org.avatar_url,
        )
        for org in orgs
    ]

    return WorkspaceSourcesResponse(sources=sources)


@router.get(
    "/{workspace_id}/stats",
    operation_id="get_workspace_stats",
    response_model=WorkspaceStatsResponse,
)
async def get_stats(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> WorkspaceStatsResponse:
    """Stat cards for the dashboard, issues and PR pages."""
    cached, version = await get_cached_stats(str(workspace_id))

    def _load(db: Session) -> WorkspaceStatsResponse | None:
        verify_workspace_access_from_path(db, current_user, workspace_id)
        if cached is not None:
            return None
        return WorkspaceStatsResponse.model_validate(db_get_workspace_stats(db, workspace_id))

    fresh = await run_in_session(_load)
    if fresh is None:
        return WorkspaceStatsResponse.model_validate(cached)
    if version is not None:
        await store_stats(str(workspace_id), version, fresh.model_dump(mode="json"))
    return fresh


def _execution_to_response(ex: Execution) -> WorkspaceExecutionResponse:
    duration = None
    completed_at = None
    if ex.status in ("completed", "failed", "cancelled") and ex.updated_at and ex.created_at:
        completed_at = ex.updated_at
        duration = (ex.updated_at - ex.created_at).total_seconds()

    # Get source provider from the issue's org (first linked issue for batched fixes),
    # falling back to the PR's org — REVIEW/SUMMARY/RESPOND-on-PR executions have no
    # linked issue at all.
    primary_issue = ex.issues[0] if ex.issues else None
    primary_pr = ex.pull_requests[0] if ex.pull_requests else None

    source = "unknown"
    source_name = None
    source_avatar_url = None
    if primary_issue and primary_issue.repository and primary_issue.repository.organization:
        org = primary_issue.repository.organization
        source = org.provider
        source_name = org.name
        source_avatar_url = org.avatar_url
    elif primary_pr and primary_pr.repository and primary_pr.repository.organization:
        org = primary_pr.repository.organization
        source = org.provider
        source_name = org.name
        source_avatar_url = org.avatar_url

    repo_name = None
    pr_url = None
    pr_number = None
    if primary_pr:
        pr_url = primary_pr.pr_url
        pr_number = primary_pr.pr_number
        if primary_pr.repository:
            repo_name = primary_pr.repository.name

    if primary_issue:
        title = primary_issue.title
        kind = "issue"
    elif primary_pr:
        title = primary_pr.title
        kind = "pull_request"
    else:
        title = "Unknown"
        kind = "unknown"

    return WorkspaceExecutionResponse(
        id=ex.id,
        issue_title=title,
        source=source,
        source_name=source_name,
        source_avatar_url=source_avatar_url,
        repo_name=repo_name,
        workflow=ex.workflow,
        kind=kind,
        status=ex.status,
        # Execution.current_step was dropped from the DB in 2026-04-20's
        # migration; nothing repopulated it, so it's always None here.
        current_step=None,
        error_type=ex.error_type,
        error_detail=ex.error_detail,
        pr_url=pr_url,
        pr_number=pr_number,
        prompt_text=ex.prompt_text,
        started_at=ex.created_at,
        completed_at=completed_at,
        duration_seconds=duration,
    )


@router.get(
    "/{workspace_id}/executions",
    operation_id="get_workspace_executions",
    response_model=WorkspaceExecutionsResponse,
)
def get_executions(
    workspace_id: uuid.UUID,
    status: str | None = None,
    page: int = 1,
    limit: int = 12,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkspaceExecutionsResponse:
    """Get paginated executions for the dashboard grid."""
    verify_workspace_access_from_path(db, current_user, workspace_id)

    executions, total = db_get_workspace_executions(
        db, workspace_id, status=status, page=page, limit=limit
    )

    return WorkspaceExecutionsResponse(
        items=[_execution_to_response(ex) for ex in executions],
        total=total,
        page=page,
        has_more=(page * limit) < total,
    )


@router.get(
    "/{workspace_id}/executions/active",
    operation_id="get_workspace_active_executions",
    response_model=ActiveExecutionsResponse,
)
async def get_active_executions(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> ActiveExecutionsResponse:
    """Running then queued executions for the dashboard, in one query."""

    def _load(db: Session) -> ActiveExecutionsResponse:
        verify_workspace_access_from_path(db, current_user, workspace_id)
        executions = db_get_workspace_active_executions(db, workspace_id)
        return ActiveExecutionsResponse(items=[_execution_to_response(ex) for ex in executions])

    return await run_in_session(_load)


@router.get(
    "/{workspace_id}/leaderboards",
    operation_id="get_workspace_leaderboards",
    response_model=LeaderboardsResponse,
)
def get_leaderboards(
    workspace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> LeaderboardsResponse:
    """Get all leaderboards for the dashboard."""
    verify_workspace_access_from_path(db, current_user, workspace_id)

    lgtm = [
        LgtmEntry(username=e["username"], avatar_url=e["avatar_url"], lgtm_count=e["lgtm_count"])  # type: ignore[arg-type]
        for e in db_get_lgtm_leaderboard(db, workspace_id)
    ]
    pr_authors = [
        PrAuthorEntry(username=e["username"], avatar_url=e["avatar_url"], pr_count=e["pr_count"])  # type: ignore[arg-type]
        for e in db_get_top_pr_authors(db, workspace_id)
    ]
    repos = [
        ActiveRepoEntry(
            name=e["name"],  # type: ignore[arg-type]
            provider=e["provider"],  # type: ignore[arg-type]
            execution_count=e["execution_count"],  # type: ignore[arg-type]
        )
        for e in db_get_active_repos(db, workspace_id)
    ]

    return LeaderboardsResponse(lgtm=lgtm, top_pr_authors=pr_authors, active_repos=repos)
