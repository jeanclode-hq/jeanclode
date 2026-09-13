"""Source projects listing, resolve, and mapping endpoints."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.database import (
    db_get_org_by_id,
    db_get_orgs_by_workspace,
    db_get_repositories_by_org,
    db_get_repository_by_id,
    db_update_repository_mapping,
    get_session,
    run_in_session,
)
from api.models import User
from api.models.repositories import MappingMethod
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.auth.dependencies import (
    get_current_user,
    verify_org_access,
)

from .schemas import (
    ResolveMappingsMessage,
    ResolveMappingsResponse,
    SentryProjectResponse,
    UpdateProjectRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["Source Projects"])


@router.get(
    "",
    operation_id="list_source_projects",
    response_model=list[SentryProjectResponse],
)
def list_source_projects(
    org_id: uuid.UUID = Query(..., description="Organization ID"),
    current_user: User = Depends(verify_org_access),
    db: Session = Depends(get_session),
) -> list[SentryProjectResponse]:
    """List synced source projects for an organization."""
    projects = db_get_repositories_by_org(db, org_id)
    return [
        SentryProjectResponse(
            id=p.id,
            external_id=p.external_id,
            name=p.name,
            mapped_repo_id=p.mapped_repo_id,
            mapping_method=p.mapping_method,
        )
        for p in projects
    ]


@router.post(
    "/resolve",
    operation_id="resolve_project_mappings",
    response_model=ResolveMappingsResponse,
    status_code=202,
)
async def resolve_mappings(
    org_id: uuid.UUID = Query(..., description="Sentry Organization ID"),
    current_user: User = Depends(verify_org_access),
) -> JSONResponse:
    """Queue auto-resolution of project-to-repo mappings.

    Publishes a single job per Sentry org. The consumer unions all git
    orgs in the same workspace, fetches Sentry code mappings once, and
    falls back to fuzzy name matching across the combined repo pool.
    """

    def _check(db: Session) -> None:
        org = db_get_org_by_id(db, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if not org.workspace_id:
            raise HTTPException(status_code=400, detail="Organization has no workspace")

        # require_token=False so a workspace whose GitLab projects all live in
        # subgroups isn't reported as having no git org linked.
        git_orgs = db_get_orgs_by_workspace(db, org.workspace_id, provider="github")
        git_orgs += db_get_orgs_by_workspace(
            db, org.workspace_id, provider="gitlab", require_token=False
        )
        if not git_orgs:
            # Surface the real problem instead of silently accepting: the user's
            # most common way to hit this is an onboarding race where the GitHub
            # org claim hasn't completed yet (org row exists with workspace_id=NULL).
            raise HTTPException(
                status_code=409,
                detail="No GitHub or GitLab organization is linked to this workspace yet.",
            )

    await run_in_session(_check)

    broker = get_faststream_broker()
    await broker.publish(
        ResolveMappingsMessage(sentry_org_id=str(org_id)),
        stream="jeanclode.events.mapping.resolve",
        maxlen=STREAM_MAXLEN,
    )

    return JSONResponse(status_code=202, content={"status": "accepted"})


@router.patch(
    "/{project_id}",
    operation_id="update_source_project",
    response_model=SentryProjectResponse,
    dependencies=[Depends(get_current_user)],
)
def update_project(
    project_id: uuid.UUID,
    request: UpdateProjectRequest,
    db: Session = Depends(get_session),
) -> SentryProjectResponse:
    """Manually set or clear the repo mapping for a source project.

    Sets mapping_method to 'manual'. Manual mappings are never
    overwritten by auto-resolution.
    """
    project = db_get_repository_by_id(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Source project not found")

    updated = db_update_repository_mapping(
        db, project_id, request.repo_id, MappingMethod.MANUAL.value
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Source project not found")

    return SentryProjectResponse(
        id=updated.id,
        external_id=updated.external_id,
        name=updated.name,
        mapped_repo_id=updated.mapped_repo_id,
        mapping_method=updated.mapping_method,
    )
