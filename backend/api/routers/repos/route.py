"""Repos listing, settings, and repo-group endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.database import (
    db_count_enabled_repositories,
    db_get_related_repos,
    db_get_related_repos_bulk,
    db_get_repositories_by_ids,
    db_get_repository_by_id,
    db_is_workspace_member,
    db_link_repos,
    db_list_repositories_by_org,
    db_set_org_repos_enabled,
    db_unlink_repos,
    get_session,
)
from api.models import User
from api.models.repositories import Repository
from api.models.settings import RepoSettings
from api.routers.auth.dependencies import (
    get_current_user,
    verify_org_access,
    verify_org_access_from_body,
)

from .schemas import (
    BulkRepoSettingsRequest,
    BulkRepoSettingsResponse,
    LinkRepoRequest,
    RelatedRepo,
    RepoPage,
    RepoResponse,
)

router = APIRouter(prefix="/repos", tags=["Repos"])


def _to_related(repo: Repository) -> RelatedRepo:
    return RelatedRepo(
        id=repo.id,
        root_org_id=repo.organization.root_org_id or repo.org_id,
        name=repo.name,
    )


def _to_response(repo: Repository, related: list[Repository] | None = None) -> RepoResponse:
    settings = RepoSettings.model_validate(repo.settings or {})
    return RepoResponse(
        id=repo.id,
        org_id=repo.org_id,
        root_org_id=repo.organization.root_org_id or repo.org_id,
        name=repo.name,
        external_id=repo.external_id,
        provider=repo.provider,
        web_url=repo.web_url,
        avatar_url=repo.avatar_url,
        enabled=settings.enabled,
        related=[_to_related(r) for r in related or []],
    )


@router.get(
    "",
    operation_id="list_repos",
    response_model=RepoPage,
)
def list_repos(
    org_id: uuid.UUID = Query(..., description="Organization ID"),
    page: int = Query(1, ge=1, description="1-indexed page"),
    limit: int = Query(25, ge=1, le=200, description="Page size"),
    search: str | None = Query(None, description="Case-insensitive name filter"),
    current_user: User = Depends(verify_org_access),
    db: Session = Depends(get_session),
) -> RepoPage:
    """One page of an organization's repositories, name-ordered.

    Paginated server-side so groups with hundreds of repos stay responsive —
    callers that genuinely need every repo page through until ``has_more`` is
    false.
    """
    repos, total = db_list_repositories_by_org(db, org_id, page=page, limit=limit, search=search)
    related_by_repo = db_get_related_repos_bulk(db, [repo.id for repo in repos])
    return RepoPage(
        items=[_to_response(repo, related_by_repo.get(repo.id, [])) for repo in repos],
        total=total,
        enabled_count=db_count_enabled_repositories(db, org_id, search),
        page=page,
        has_more=(page * limit) < total,
    )


@router.get(
    "/lookup",
    operation_id="lookup_repos",
    response_model=list[RelatedRepo],
)
def lookup_repos(
    ids: list[uuid.UUID] = Query(default_factory=list, description="Repository IDs to resolve"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RelatedRepo]:
    """Resolve repository ids to names, for settings that store ids.

    The always-include list holds ids so a rename can't stale it out, which
    leaves the picker with nothing to render until they're resolved. Only
    repos in an org the caller can reach come back; an id they can't see is
    dropped rather than refused, so one stale entry can't blank the picker.
    """
    repos = db_get_repositories_by_ids(db, ids)
    return [
        _to_related(repo)
        for repo in repos
        if repo.organization.workspace_id
        and db_is_workspace_member(db, repo.organization.workspace_id, current_user.id)
    ]


@router.patch(
    "/settings",
    operation_id="bulk_update_repo_settings",
    response_model=BulkRepoSettingsResponse,
)
def bulk_update_repo_settings(
    request: BulkRepoSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BulkRepoSettingsResponse:
    """Enable or disable triggers on every repository of an organization.

    ``search`` narrows the set to exactly what the dashboard's repo filter is
    showing, so the bulk toggle never reaches repos the user can't see.
    """
    verify_org_access_from_body(db, current_user, request.org_id)

    updated = db_set_org_repos_enabled(
        db, request.org_id, enabled=request.enabled, search=request.search
    )
    return BulkRepoSettingsResponse(updated=updated)


@router.patch(
    "/{repo_id}/settings",
    operation_id="update_repo_settings",
    response_model=RepoSettings,
)
def update_repo_settings(
    repo_id: uuid.UUID,
    request: RepoSettings,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RepoSettings:
    """Update settings for a repository (e.g. enable/disable triggers)."""
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    # Verify access via the repo's parent git org
    verify_org_access_from_body(db, current_user, repo.org_id)

    repo.settings = request.model_dump()
    db.commit()
    db.refresh(repo)

    return RepoSettings.model_validate(repo.settings)


@router.post(
    "/{repo_id}/related",
    operation_id="link_related_repo",
    response_model=list[RelatedRepo],
)
def link_related_repo(
    repo_id: uuid.UUID,
    request: LinkRepoRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RelatedRepo]:
    """Group two GitHub/GitLab repos together — both get cloned into the same
    workspace whenever either one is worked on. Manual only: unlike Sentry
    project mapping, there's no auto-resolution signal for "these repos
    belong together".

    The two repos may sit in different git orgs when both are GitLab repos in
    the same workspace (see ``db_link_repos``); the caller must have access to
    both orgs."""
    repo = db_get_repository_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    verify_org_access_from_body(db, current_user, repo.org_id)

    related_repo = db_get_repository_by_id(db, request.related_repo_id)
    if not related_repo:
        raise HTTPException(status_code=404, detail="Related repository not found")
    verify_org_access_from_body(db, current_user, related_repo.org_id)

    try:
        db_link_repos(db, repo_id, request.related_repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    related = db_get_related_repos(db, repo_id)
    return [_to_related(r) for r in related]


@router.delete(
    "/{repo_id}/related/{related_repo_id}",
    operation_id="unlink_related_repo",
    response_model=list[RelatedRepo],
)
def unlink_related_repo(
    repo_id: uuid.UUID,
    related_repo_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RelatedRepo]:
    """Remove a repo-group link between two repos."""
    repo = db_get_repository_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    verify_org_access_from_body(db, current_user, repo.org_id)

    db_unlink_repos(db, repo_id, related_repo_id)

    related = db_get_related_repos(db, repo_id)
    return [_to_related(r) for r in related]
