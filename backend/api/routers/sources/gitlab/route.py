"""GitLab sources endpoint — add GitLab group or project tokens."""

import logging
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.context import get_current_app
from api.database import (
    db_create_org,
    db_create_repository,
    db_create_workspace,
    db_ensure_org_membership,
    db_get_descendant_orgs,
    db_get_org_by_external_id,
    db_get_repository_by_org_and_external_id,
    db_upsert_org_with_ancestors,
)
from api.models import User
from api.models.organizations import OrgMembership
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.plugins.gitlab.schemas import GitlabTokenType
from api.routers.auth.dependencies import get_current_user
from api.routers.orgs.schemas import SyncOrgMembersMessage

from .schemas import (
    AddGitLabSourceRequest,
    GitLabSourceResponse,
    GitLabSyncGroupRepositoriesMessage,
    GitLabSyncProjectRepositoryMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["Sources"])


def _new_org_settings(request: AddGitLabSourceRequest) -> dict | None:
    """Seed settings for a freshly created org from the add-source request."""
    return {"manage_project_webhooks": True} if request.manage_project_webhooks else None


def _extract_group_id_from_username(username: str) -> str:
    """Extract group ID from a GitLab group bot username.

    Group bot usernames follow the pattern: group_{id}_bot_{hash}
    """
    match = re.match(r"group_(\d+)_bot_", username)
    if not match:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract group ID from bot username: {username}",
        )
    return match.group(1)


def _extract_project_id_from_username(username: str) -> str:
    """Extract project ID from a GitLab project bot username.

    Project bot usernames follow the pattern: project_{id}_bot_{hash}
    """
    match = re.match(r"project_(\d+)_bot_", username)
    if not match:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract project ID from bot username: {username}",
        )
    return match.group(1)


@router.post(
    "/gitlab",
    operation_id="add_gitlab_source",
    response_model=GitLabSourceResponse,
)
async def add_gitlab_source(
    request: AddGitLabSourceRequest,
    current_user: User = Depends(get_current_user),
) -> GitLabSourceResponse:
    """Add a GitLab source by providing a Group or Project Access Token.

    This endpoint:
    1. Verifies the GitLab token is valid
    2. Determines if it's a group or project token
    3. Creates Organization (+ single repo for project tokens) with encrypted token
    4. Queues background repo sync (group tokens only)
    """
    app = get_current_app()
    gitlab_plugin = app.gitlab

    if not gitlab_plugin:
        raise HTTPException(status_code=404, detail="GitLab integration not configured")

    # Verify the token
    try:
        token_data = await gitlab_plugin.verify_token(
            request.access_token, provider_url=request.gitlab_url
        )
    except HTTPException as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Failed to verify GitLab token: {e.detail}",
        ) from e

    token_type = gitlab_plugin.get_token_type(token_data)

    if token_type == GitlabTokenType.GROUP_ACCESS_TOKEN:
        return await _handle_group_token(request, token_data, app, current_user)
    elif token_type == GitlabTokenType.PROJECT_ACCESS_TOKEN:
        return await _handle_project_token(request, token_data, app, current_user)
    else:
        raise HTTPException(
            status_code=400,
            detail="Token must be a group or project access token.",
        )


async def _handle_group_token(request, token_data, app, current_user):
    """Handle a group access token — creates org and syncs all repos."""
    gitlab_plugin = app.gitlab
    db_plugin = app.database
    if not db_plugin:
        raise HTTPException(status_code=503, detail="Database not configured")

    group_id = _extract_group_id_from_username(token_data["username"])
    group_info = await gitlab_plugin.fetch_group(
        request.access_token, group_id, provider_url=request.gitlab_url
    )
    ancestors = await gitlab_plugin.fetch_group_ancestors(
        request.access_token,
        group_id,
        provider_url=request.gitlab_url,
        known_parent_id=group_info.get("parent_id"),
    )

    encrypted_token = db_plugin.encrypt(request.access_token)
    external_org_id = str(group_info["id"])
    group_name = group_info["name"]
    installation_id = f"gitlab-group-{group_info['id']}"

    with db_plugin.session() as db:
        existing_org = db_get_org_by_external_id(
            db, external_org_id, provider="gitlab", base_url=request.gitlab_url
        )
        if existing_org:
            # US-3: Upgrade — set group token on org, clear project-level repo tokens
            existing_org.auth_token_encrypted = encrypted_token
            existing_org.installation_id = installation_id
            if request.manage_project_webhooks:
                existing_org.settings = {
                    **(existing_org.settings or {}),
                    "manage_project_webhooks": True,
                }
            for repo in existing_org.repositories:
                repo.auth_token_encrypted = None

            # A subgroup connected earlier (its own group/subgroup token) may
            # sit below this org — that token is now redundant, since this
            # ancestor's token already covers it via GitLab's inherited
            # permissions. Demote it back to a placeholder, same idea as
            # clearing a repo's project token above one level down.
            if existing_org.workspace_id:
                for descendant in db_get_descendant_orgs(
                    db, existing_org.id, existing_org.workspace_id
                ):
                    if descendant.auth_token_encrypted:
                        descendant.auth_token_encrypted = None
                        descendant.installation_id = None

            for identity in current_user.identities:
                db_ensure_org_membership(db, existing_org.id, identity.id, role="owner")
            db.commit()
            org_id = str(existing_org.id)
            org_name = existing_org.name
        else:
            if request.workspace_id:
                workspace_id = UUID(request.workspace_id)
            else:
                workspace = db_create_workspace(db, name=group_name, slug=group_name.lower())
                workspace_id = workspace.id

            parent_org_id, root_org_id = db_upsert_org_with_ancestors(
                db=db,
                workspace_id=workspace_id,
                provider="gitlab",
                ancestors=ancestors,
                base_url=request.gitlab_url,
            )

            org = db_create_org(
                db=db,
                workspace_id=workspace_id,
                name=group_name,
                external_org_id=external_org_id,
                provider="gitlab",
                installation_id=installation_id,
                base_url=request.gitlab_url,
                auth_token_encrypted=encrypted_token,
                avatar_url=group_info.get("avatar_url"),
                parent_org_id=parent_org_id,
                root_org_id=root_org_id,
                settings=_new_org_settings(request),
            )
            org_id = str(org.id)
            org_name = org.name

            for identity in current_user.identities:
                membership = OrgMembership(
                    org_id=org.id,
                    provider_identity_id=identity.id,
                    role="owner",
                )
                db.add(membership)
            db.commit()

    # Queue background repo sync
    repos_synced = False
    try:
        broker = get_faststream_broker()
        await broker.publish(
            GitLabSyncGroupRepositoriesMessage(
                org_id=org_id,
                group_id=group_id,
            ),
            stream="jeanclode.events.gitlab.sync_group_repositories",
            maxlen=STREAM_MAXLEN,
        )
        repos_synced = True
    except Exception as e:
        logger.error(f"Failed to queue repo sync for group {org_name}: {e}")

    # Queue background member pre-population
    try:
        broker = get_faststream_broker()
        await broker.publish(
            SyncOrgMembersMessage(org_id=org_id, provider="gitlab"),
            stream="jeanclode.events.orgs.sync_members",
            maxlen=STREAM_MAXLEN,
        )
    except Exception as e:
        logger.error(f"Failed to queue member sync for group {org_name}: {e}")

    return GitLabSourceResponse(
        source_type="group",
        source_id=org_id,
        source_name=org_name,
        repos_synced=repos_synced,
    )


async def _handle_project_token(request, token_data, app, current_user):
    """Handle a project access token — creates org + single repo."""
    gitlab_plugin = app.gitlab
    db_plugin = app.database
    if not db_plugin:
        raise HTTPException(status_code=503, detail="Database not configured")

    project_id = _extract_project_id_from_username(token_data["username"])
    project_info = await gitlab_plugin.fetch_project(
        request.access_token, project_id, provider_url=request.gitlab_url
    )

    encrypted_token = db_plugin.encrypt(request.access_token)

    # Resolve the namespace to use as the org (group or user namespace)
    namespace = project_info.get("namespace", {})
    namespace_id = str(namespace.get("id", project_id))
    namespace_name = namespace.get("name", project_info["name"])
    namespace_kind = namespace.get("kind", "group")

    # Fetch ancestors only for group namespaces (not personal user namespaces)
    ancestors: list[dict] = []
    if namespace_kind != "user":
        ancestors = await gitlab_plugin.fetch_group_ancestors(
            request.access_token, namespace_id, provider_url=request.gitlab_url
        )

    external_repo_id = str(project_info["id"])

    with db_plugin.session() as db:
        existing_org = db_get_org_by_external_id(
            db, namespace_id, provider="gitlab", base_url=request.gitlab_url
        )
        if existing_org:
            # US-7: Namespace org already exists — reuse it
            org_id = str(existing_org.id)
            org_name = existing_org.name
            # If org already has a group token, repo inherits — don't store token on repo
            repo_token = None if existing_org.auth_token_encrypted else encrypted_token
            if request.manage_project_webhooks:
                existing_org.settings = {
                    **(existing_org.settings or {}),
                    "manage_project_webhooks": True,
                }
            for identity in current_user.identities:
                db_ensure_org_membership(db, existing_org.id, identity.id, role="owner")
        else:
            if request.workspace_id:
                workspace_id = UUID(request.workspace_id)
            else:
                workspace = db_create_workspace(
                    db, name=namespace_name, slug=namespace_name.lower()
                )
                workspace_id = workspace.id

            parent_org_id, root_org_id = db_upsert_org_with_ancestors(
                db=db,
                workspace_id=workspace_id,
                provider="gitlab",
                ancestors=ancestors,
                base_url=request.gitlab_url,
            )

            org = db_create_org(
                db=db,
                workspace_id=workspace_id,
                name=namespace_name,
                external_org_id=namespace_id,
                provider="gitlab",
                installation_id=f"gitlab-project-{project_id}",
                base_url=request.gitlab_url,
                avatar_url=namespace.get("avatar_url"),
                parent_org_id=parent_org_id,
                root_org_id=root_org_id,
                settings=_new_org_settings(request),
            )
            org_id = str(org.id)
            org_name = org.name

            for identity in current_user.identities:
                membership = OrgMembership(
                    org_id=org.id,
                    provider_identity_id=identity.id,
                    role="owner",
                )
                db.add(membership)
            db.commit()
            repo_token = encrypted_token

        # Create the single repo, or backfill its token if a tokenless row
        # already exists (e.g. left over from a prior sync before a token
        # was ever supplied) — re-submitting a token must not be a no-op.
        existing_repo = db_get_repository_by_org_and_external_id(db, UUID(org_id), external_repo_id)
        if not existing_repo:
            db_create_repository(
                db=db,
                org_id=UUID(org_id),
                external_id=external_repo_id,
                name=project_info["path_with_namespace"],
                web_url=project_info["web_url"],
                provider="gitlab",
                provider_url=request.gitlab_url or "https://gitlab.com",
                auth_token_encrypted=repo_token,
                avatar_url=project_info.get("avatar_url"),
            )
        elif not existing_repo.auth_token_encrypted and repo_token:
            existing_repo.auth_token_encrypted = repo_token
            db.commit()

    # Queue background MR + issue backfill for the single project
    repos_synced = False
    try:
        broker = get_faststream_broker()
        await broker.publish(
            GitLabSyncProjectRepositoryMessage(
                org_id=org_id,
                project_id=external_repo_id,
            ),
            stream="jeanclode.events.gitlab.sync_project_repository",
            maxlen=STREAM_MAXLEN,
        )
        repos_synced = True
    except Exception as e:
        logger.error(f"Failed to queue repo sync for project {external_repo_id}: {e}")

    return GitLabSourceResponse(
        source_type="project",
        source_id=org_id,
        source_name=org_name,
        repos_synced=repos_synced,
    )
