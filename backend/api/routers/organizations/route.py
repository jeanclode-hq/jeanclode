"""Organization endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import (
    db_count_org_repositories,
    db_delete_org_by_id,
    db_get_descendant_orgs,
    db_get_org_by_id,
    db_get_org_by_installation_id,
    db_get_org_members,
    db_get_org_stats,
    db_get_source_orgs,
    db_get_subgroup_repo_counts,
    db_is_workspace_member,
    db_resolve_org_token,
    db_update_org,
    get_session,
    run_in_session,
)
from api.database.organization import db_resolve_org_settings
from api.models import User
from api.models.organizations import Organization, OrgMembership
from api.models.settings import OrgSettings, get_settings_model, merge_settings
from api.plugins.container.related import MAX_PACKED_SUBGROUP_REPOS
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.auth.dependencies import (
    get_current_user,
    verify_org_access_from_body,
)
from api.routers.sources.gitlab.schemas import (
    GitLabProjectHookTeardown,
    GitLabProjectHookTeardownMessage,
    GitLabSyncGroupRepositoriesMessage,
    GitLabSyncProjectRepositoryMessage,
)
from api.routers.webhooks.github.schemas import GitHubSyncInstallationMessage

from .schemas import (
    ClaimOrgRequest,
    OrgMemberResponse,
    OrgMembersResponse,
    OrgResponse,
    OrgStatsResponse,
    OrgSubgroupResponse,
    OrgSubgroupsResponse,
    SyncOrgRepositoriesResponse,
    UpdateOrgRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations", tags=["Organizations"])


def _to_response(db: Session, org: Organization) -> OrgResponse:
    # Repositories are counted across the org's subtree: a GitLab group's
    # projects hang off an org per subgroup namespace, so its own rows are
    # only part of what the connection covers.
    repo_count = (
        db_count_org_repositories(db, org.id, org.workspace_id)
        if org.workspace_id is not None
        else len(org.repositories or [])
    )
    return OrgResponse(
        id=org.id,
        name=org.name,
        provider=org.provider,
        external_org_id=org.external_org_id,
        base_url=org.base_url,
        avatar_url=org.avatar_url,
        onboarding_step=org.onboarding_step,
        repo_count=repo_count,
        created_at=org.created_at.isoformat(),
    )


@router.get(
    "",
    operation_id="list_organizations",
    response_model=list[OrgResponse],
)
def list_organizations(
    workspace_id: uuid.UUID,
    provider: list[str] | None = Query(None, description="Filter by provider(s)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[OrgResponse]:
    """List all organizations in a workspace, optionally filtered by provider.

    Supports multiple values: ``?provider=github&provider=gitlab``
    """
    if not db_is_workspace_member(db, workspace_id, current_user.id):
        raise HTTPException(status_code=403, detail="Not a member of this workspace")

    # One card per connection, matching the source tabs on the issues and PR
    # pages: the org a credential was attached to, never the subgroups a
    # GitLab group token already reaches (see db_get_source_orgs).
    orgs = db_get_source_orgs(db, workspace_id)
    if provider:
        orgs = [org for org in orgs if org.provider in provider]

    return [_to_response(db, org) for org in orgs]


@router.post(
    "/claim",
    operation_id="claim_organization",
    response_model=OrgResponse,
)
def claim_organization(
    request: ClaimOrgRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgResponse:
    """Claim an orphaned organization into a workspace.

    After a GitHub App installation, the webhook creates an Organization with
    no workspace. The frontend calls this to assign it to the user's
    current workspace.
    """
    if not db_is_workspace_member(db, request.workspace_id, current_user.id):
        raise HTTPException(status_code=403, detail="Not a member of this workspace")

    org = db_get_org_by_installation_id(db, request.installation_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found for this installation")

    if org.workspace_id is not None and org.workspace_id != request.workspace_id:
        raise HTTPException(
            status_code=409, detail="Organization already belongs to another workspace"
        )

    if org.workspace_id is None:
        org = db_update_org(db, org.id, workspace_id=str(request.workspace_id))
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

    # Create membership for the claiming user
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
            db.add(
                OrgMembership(
                    org_id=org.id,
                    provider_identity_id=identity.id,
                    role="owner",
                )
            )
    db.commit()

    return _to_response(db, org)


@router.get(
    "/{org_id}",
    operation_id="get_organization",
    response_model=OrgResponse,
)
def get_organization(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgResponse:
    """Get organization details."""
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    return _to_response(db, org)


@router.patch(
    "/{org_id}",
    operation_id="update_organization",
    response_model=OrgResponse,
)
def update_organization(
    org_id: uuid.UUID,
    request: UpdateOrgRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgResponse:
    """Update an organization."""
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    updates = request.model_dump(exclude_none=True)
    if updates:
        org = db_update_org(db, org_id, **updates)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

    return _to_response(db, org)


@router.delete(
    "/{org_id}",
    operation_id="delete_organization",
    status_code=200,
)
async def delete_organization(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Delete an organization and uninstall from the provider if applicable."""

    def _read(db: Session) -> tuple[str, str, str | None, list[GitLabProjectHookTeardown]]:
        verify_org_access_from_body(db, current_user, org_id)
        org = db_get_org_by_id(db, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Captured while the rows still exist — the teardown message has to
        # carry every project and token, since the org is gone by the time
        # the consumer reads it.
        teardown: list[GitLabProjectHookTeardown] = []
        if org.provider == "gitlab" and db_resolve_org_settings(db, org).get(
            "manage_project_webhooks"
        ):
            teardown = _collect_gitlab_project_hook_teardown(db, org)

        return org.name, org.provider, org.installation_id, teardown

    name, provider, installation_id, teardown = await run_in_session(_read)

    # Uninstall GitHub App if applicable
    if provider == "github" and installation_id:
        app = get_current_app()
        if app.github:
            try:
                await app.github.delete_installation(installation_id)
            except Exception as e:
                logger.warning(f"Failed to uninstall GitHub App {installation_id}: {e}")

    # Remove the project webhooks we created — queued, since the rows go now and
    # the GitLab API calls shouldn't hold up the response.
    await _publish_gitlab_project_hook_teardown(teardown, org_id)

    await run_in_session(lambda db: db_delete_org_by_id(db, org_id))
    return {"message": f"Organization {name} deleted"}


@router.post(
    "/{org_id}/sync-repositories",
    operation_id="sync_organization_repositories",
    response_model=SyncOrgRepositoriesResponse,
    status_code=202,
)
async def sync_organization_repositories(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> SyncOrgRepositoriesResponse:
    """Re-scan the provider for repositories this org should have.

    Webhooks are the normal path, but a dropped delivery — or a GitLab
    instance with no system hook configured — leaves projects the org owns
    with no row at all. This runs the same sync the connection ran, which
    creates what is missing, repairs what is stale, and backfills open
    PRs/MRs and issues.
    """

    def _read(db: Session) -> tuple[object, str, str, str]:
        verify_org_access_from_body(db, current_user, org_id)

        org = db_get_org_by_id(db, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        if not org.workspace_id:
            raise HTTPException(
                status_code=400, detail="Organization is not attached to a workspace"
            )

        if org.provider == "github":
            if not org.installation_id:
                raise HTTPException(
                    status_code=400, detail="Organization has no GitHub App installation"
                )
            message: object = GitHubSyncInstallationMessage(installation_id=org.installation_id)
            stream = "jeanclode.events.github.sync_installation"
        elif org.provider == "gitlab":
            if not db_resolve_org_token(db, org):
                raise HTTPException(
                    status_code=400,
                    detail="No GitLab token available for this organization or its parents",
                )
            message = GitLabSyncGroupRepositoriesMessage(
                org_id=str(org.id), group_id=org.external_org_id
            )
            stream = "jeanclode.events.gitlab.sync_group_repositories"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Repository sync is not supported for provider '{org.provider}'",
            )

        return message, stream, org.provider, org.name

    message, stream, provider, org_name = await run_in_session(_read)

    try:
        broker = get_faststream_broker()
        await broker.publish(message, stream=stream, maxlen=STREAM_MAXLEN)
    except Exception as e:
        logger.error(f"Failed to queue repo sync for org {org_id}: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not queue the sync job") from e

    return SyncOrgRepositoriesResponse(
        queued=True,
        provider=provider,
        message=f"Syncing repositories for {org_name}",
    )


@router.get(
    "/{org_id}/members",
    operation_id="list_organization_members",
    response_model=OrgMembersResponse,
)
def list_organization_members(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgMembersResponse:
    """List the org's provider members — the candidates for the notify picker.

    Not the same set as ``list_workspace_members``, which only returns
    people who have logged into Jeanclode. ``sync_org_members``
    pre-populates an identity per provider member, so the maintainer who
    has never opened the dashboard is still selectable here.
    """
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    members = [
        OrgMemberResponse(
            provider_identity_id=identity.id,
            username=identity.username or "",
            avatar_url=identity.avatar_url,
            provider=identity.provider,
            role=membership.role,
            has_account=identity.user_id is not None,
        )
        for membership, identity in db_get_org_members(db, org)
        if identity.username
    ]
    return OrgMembersResponse(members=members, total=len(members))


@router.get(
    "/{org_id}/subgroups",
    operation_id="list_organization_subgroups",
    response_model=OrgSubgroupsResponse,
)
def list_organization_subgroups(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgSubgroupsResponse:
    """Subgroups below this org, with their direct repo counts.

    Feeds the subgroup-pack exclusion picker: only the connected group is
    configurable, so this is where the tenant sees which subgroups exist and
    how big each one is before deciding which to leave out.
    """
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrgSubgroupsResponse(
        items=[
            OrgSubgroupResponse(id=subgroup.id, name=subgroup.name, repo_count=count)
            for subgroup, count in db_get_subgroup_repo_counts(db, org)
        ],
        max_pack_size=MAX_PACKED_SUBGROUP_REPOS,
    )


@router.get(
    "/{org_id}/settings",
    operation_id="get_organization_settings",
    response_model=OrgSettings,
)
def get_organization_settings(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgSettings:
    """Get settings for an organization (fills defaults for missing keys)."""
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    settings_cls = get_settings_model(org.provider)
    return settings_cls.model_validate(org.settings or {})  # type: ignore[return-value]


@router.patch(
    "/{org_id}/settings",
    operation_id="update_organization_settings",
    response_model=OrgSettings,
)
async def update_organization_settings(
    org_id: uuid.UUID,
    request: OrgSettings,
    current_user: User = Depends(get_current_user),
) -> OrgSettings:
    """Update settings for an organization.

    Merges the request into the stored settings rather than replacing them,
    so a caller that sends one field doesn't reset the rest to their
    defaults.
    """

    def _write(
        db: Session,
    ) -> tuple[OrgSettings, _HookSyncPlan | None, list[GitLabProjectHookTeardown]]:
        verify_org_access_from_body(db, current_user, org_id)

        org = db_get_org_by_id(db, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        settings_cls = get_settings_model(org.provider)
        # exclude_unset keeps fields the caller omitted out of the merge, so
        # Pydantic's filled-in defaults don't overwrite stored values.
        incoming = request.model_dump(mode="json", exclude_unset=True)
        was_managing_hooks = bool((org.settings or {}).get("manage_project_webhooks"))
        merged = settings_cls.model_validate(merge_settings(org.settings or {}, incoming))

        org = db_update_org(db, org_id, settings=merged.model_dump(mode="json"))
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        now_managing_hooks = bool((org.settings or {}).get("manage_project_webhooks"))
        hook_sync: _HookSyncPlan | None = None
        teardown: list[GitLabProjectHookTeardown] = []
        if org.provider == "gitlab" and now_managing_hooks and not was_managing_hooks:
            hook_sync = _collect_gitlab_project_hook_sync(org)
        elif org.provider == "gitlab" and was_managing_hooks and not now_managing_hooks:
            teardown = _collect_gitlab_project_hook_teardown(db, org)

        return settings_cls.model_validate(org.settings), hook_sync, teardown  # type: ignore[return-value]

    settings, hook_sync, teardown = await run_in_session(_write)

    if hook_sync is not None:
        await _publish_gitlab_project_hook_sync(hook_sync)
    await _publish_gitlab_project_hook_teardown(teardown, org_id)

    return settings


class _HookSyncPlan(NamedTuple):
    """What to publish so ``manage_project_webhooks`` takes effect now."""

    org_id: str
    group_id: str | None
    project_ids: list[str]


def _collect_gitlab_project_hook_sync(org: Organization) -> _HookSyncPlan:
    """Read what the sync needs off ``org`` while its session is still open.

    A group org re-scans all its projects; a project org re-syncs its own
    repos, which means walking ``org.repositories`` — a lazy load, so it has
    to happen here rather than next to the publish.
    """
    is_group = (org.installation_id or "").startswith("gitlab-group-")
    return _HookSyncPlan(
        org_id=str(org.id),
        group_id=org.external_org_id if is_group else None,
        project_ids=(
            [] if is_group else [r.external_id for r in org.repositories if r.provider == "gitlab"]
        ),
    )


async def _publish_gitlab_project_hook_sync(plan: _HookSyncPlan) -> None:
    try:
        broker = get_faststream_broker()
        if plan.group_id is not None:
            await broker.publish(
                GitLabSyncGroupRepositoriesMessage(org_id=plan.org_id, group_id=plan.group_id),
                stream="jeanclode.events.gitlab.sync_group_repositories",
                maxlen=STREAM_MAXLEN,
            )
        else:
            for project_id in plan.project_ids:
                await broker.publish(
                    GitLabSyncProjectRepositoryMessage(org_id=plan.org_id, project_id=project_id),
                    stream="jeanclode.events.gitlab.sync_project_repository",
                    maxlen=STREAM_MAXLEN,
                )
    except Exception as e:
        logger.error(f"Failed to queue project hook sync for org {plan.org_id}: {e}", exc_info=True)


def _collect_gitlab_project_hook_teardown(
    db: Session, org: Organization
) -> list[GitLabProjectHookTeardown]:
    """Build the teardown items for every project the org covers.

    Each project carries its own token — resolved org-chain-first, then the
    repo's own token — because a project-token setup keeps the token on the
    repo row. Collected before the publish, and before any delete: the org
    rows are gone by the time the message runs when it's fired from
    ``delete_organization``.
    """
    orgs = [org]
    if org.workspace_id:
        orgs += db_get_descendant_orgs(db, org.id, org.workspace_id)

    items: list[GitLabProjectHookTeardown] = []
    for sub_org in orgs:
        org_token = db_resolve_org_token(db, sub_org)
        for repo in sub_org.repositories:
            if repo.provider != "gitlab":
                continue
            encrypted = org_token or repo.auth_token_encrypted
            if not encrypted:
                logger.warning(
                    "No token for GitLab project %s — cannot remove its webhook",
                    repo.external_id,
                )
                continue
            items.append(
                GitLabProjectHookTeardown(
                    project_id=repo.external_id,
                    provider_url=repo.provider_url or sub_org.base_url or "https://gitlab.com",
                    encrypted_token=encrypted,
                )
            )

    return items


async def _publish_gitlab_project_hook_teardown(
    items: list[GitLabProjectHookTeardown], org_id: uuid.UUID
) -> None:
    if not items:
        return

    try:
        broker = get_faststream_broker()
        await broker.publish(
            GitLabProjectHookTeardownMessage(projects=items),
            stream="jeanclode.events.gitlab.project_hook_teardown",
            maxlen=STREAM_MAXLEN,
        )
    except Exception as e:
        logger.error(f"Failed to queue project hook teardown for org {org_id}: {e}", exc_info=True)


@router.get(
    "/{org_id}/stats",
    operation_id="get_organization_stats",
    response_model=OrgStatsResponse,
)
def get_stats(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgStatsResponse:
    """Get per-organization stats for the dashboard."""
    verify_org_access_from_body(db, current_user, org_id)

    org = db_get_org_by_id(db, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    stats = db_get_org_stats(db, org_id, org.provider)
    return OrgStatsResponse(
        total_issues=int(stats.get("total_issues", 0)),  # type: ignore[arg-type]
        issues_fixing=int(stats.get("issues_fixing", 0)),  # type: ignore[arg-type]
        issues_fixed=int(stats.get("issues_fixed", 0)),  # type: ignore[arg-type]
        issues_failed=int(stats.get("issues_failed", 0)),  # type: ignore[arg-type]
        fix_success_rate=float(stats.get("fix_success_rate", 0.0)),  # type: ignore[arg-type]
        repo_count=int(stats.get("repo_count", 0)),  # type: ignore[arg-type]
        total_prs=int(stats.get("total_prs", 0)),  # type: ignore[arg-type]
        prs_reviewed=int(stats.get("prs_reviewed", 0)),  # type: ignore[arg-type]
        prs_open=int(stats.get("prs_open", 0)),  # type: ignore[arg-type]
        latest_title=stats.get("latest_title"),  # type: ignore[arg-type]
        latest_pr_url=stats.get("latest_pr_url"),  # type: ignore[arg-type]
        latest_pr_number=stats.get("latest_pr_number"),  # type: ignore[arg-type]
        latest_at=stats.get("latest_at"),  # type: ignore[arg-type]
    )
