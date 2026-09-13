"""FastStream consumer for user membership sync after OAuth login.

Syncs organization memberships for GitHub and GitLab users
based on their provider access.
"""

import logging
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub

from api.app import Application
from api.context import get_current_app
from api.database import (
    db_get_org_by_installation_id,
    db_get_repositories_by_org,
    db_list_gitlab_orgs,
)
from api.database.identity import db_get_identity_by_external_id
from api.database.workspace import db_ensure_workspace_membership
from api.models.organizations import OrgMembership
from api.routers.auth.schemas import SyncUserMembershipsMessage
from api.sse.publishers import publish_sync_event

logger = logging.getLogger(__name__)

router = RedisRouter()


def _sync_org_membership(
    app: Application,
    provider_identity_id: UUID,
    org_id: UUID,
    workspace_id: UUID | None,
    role: str,
) -> None:
    """Create or update a single org membership and ensure workspace membership."""
    if not app.database:
        return
    with app.database.session() as db:
        existing = (
            db.query(OrgMembership)
            .filter(
                OrgMembership.org_id == org_id,
                OrgMembership.provider_identity_id == provider_identity_id,
            )
            .first()
        )
        if existing:
            existing.role = role
        else:
            membership = OrgMembership(
                org_id=org_id,
                provider_identity_id=provider_identity_id,
                role=role,
            )
            db.add(membership)
        db.commit()

        # Also ensure the user is a member of the workspace
        if workspace_id:
            db_ensure_workspace_membership(db, workspace_id, provider_identity_id)


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.auth.sync_memberships",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def sync_user_memberships(message: SyncUserMembershipsMessage) -> None:
    """Sync organization memberships for a user after OAuth login.

    For GitHub: fetches user's app installations → matches to known orgs
    For GitLab: fetches user's groups → matches to known orgs
    """
    user_id = message.user_id
    provider = message.provider

    logger.info(f"Starting membership sync for user {user_id} ({provider})")

    app = get_current_app()

    if not app.database:
        logger.error("Database plugin not available")
        return

    # Phase 1: DB read — get provider identity
    with app.database.session() as db:
        provider_identity = db_get_identity_by_external_id(
            db=db,
            provider=provider,
            external_id=message.external_user_id,
        )

        if not provider_identity:
            logger.error(f"Provider identity not found for {provider}:{message.external_user_id}")
            return

        provider_identity_id = provider_identity.id

    # Phase 2: Provider-specific sync
    try:
        if provider == "github":
            try:
                access_token = app.database.decrypt(message.access_token_encrypted)
            except Exception as e:
                logger.error(f"Failed to decrypt access token: {e}")
                return
            await _sync_github_memberships(app, provider_identity_id, access_token)
        elif provider == "gitlab":
            await _sync_gitlab_memberships(app, provider_identity_id, message.external_user_id)
        else:
            logger.warning(f"Unknown provider: {provider}")
    except Exception as e:
        logger.error(f"Failed to sync memberships for user {user_id}: {e}", exc_info=True)


async def _sync_github_memberships(
    app: Application,
    provider_identity_id: UUID,
    access_token: str,
) -> None:
    """Sync GitHub organization memberships via user installations."""
    github_plugin = app.github
    if not github_plugin:
        logger.warning("GitHub plugin not available for membership sync")
        return

    # Fetch installations the user has access to
    try:
        headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await github_plugin.http.get("/user/installations", headers=headers)
        if response.status_code != 200:
            logger.error(f"GitHub /user/installations returned {response.status_code}")
            return
        installations = response.json().get("installations", [])
    except Exception as e:
        logger.error(f"Failed to fetch user installations: {e}")
        return

    logger.debug(f"User has access to {len(installations)} GitHub App installations")

    if not app.database:
        return

    orgs_synced = 0
    workspace_ids: set[str] = set()
    for installation in installations:
        installation_id = str(installation["id"])

        with app.database.session() as db:
            org = db_get_org_by_installation_id(db, installation_id)

        if not org:
            continue

        _sync_org_membership(app, provider_identity_id, org.id, org.workspace_id, "member")
        orgs_synced += 1
        if org.workspace_id:
            workspace_ids.add(str(org.workspace_id))

    logger.info(f"GitHub sync complete: {orgs_synced} orgs")

    for ws_id in workspace_ids:
        await publish_sync_event(
            workspace_id=ws_id,
            action="memberships_synced",
            payload={"provider": "github"},
        )


def _gitlab_access_level_to_role(access_level: int) -> str:
    """Map GitLab access_level integer to a Jeanclode role string."""
    if access_level >= 50:
        return "owner"
    elif access_level >= 40:
        return "admin"
    else:
        return "member"


async def _sync_gitlab_memberships(
    app: Application,
    provider_identity_id: UUID,
    external_user_id: str,
) -> None:
    """Sync GitLab memberships using each org's stored token.

    For each GitLab org in the system:
    - Group/subgroup token (org.auth_token_encrypted): calls groups/{id}/members/all
    - Project token (repo.auth_token_encrypted on first repo): calls projects/{id}/members/all
    - Placeholder/ancestor orgs with no usable token: skipped
    """
    gitlab_plugin = app.gitlab
    if not gitlab_plugin:
        logger.warning("GitLab plugin not available for membership sync")
        return

    if not app.database:
        return

    # Collect org data while the session is open, then close it before making
    # remote API calls to avoid holding the connection during network I/O.
    org_entries = []
    with app.database.session() as db:
        for org in db_list_gitlab_orgs(db):
            org_token = org.auth_token_encrypted
            repo_token: str | None = None
            repo_external_id: str | None = None
            provider_url = org.base_url

            if not org_token:
                for repo in db_get_repositories_by_org(db, org.id):
                    if repo.auth_token_encrypted:
                        repo_token = repo.auth_token_encrypted
                        repo_external_id = repo.external_id
                        if repo.provider_url:
                            provider_url = repo.provider_url
                        break

            org_entries.append(
                {
                    "id": org.id,
                    "external_org_id": org.external_org_id,
                    "workspace_id": org.workspace_id,
                    "org_token": org_token,
                    "repo_token": repo_token,
                    "repo_external_id": repo_external_id,
                    "provider_url": provider_url,
                }
            )

    logger.debug(f"Checking {len(org_entries)} GitLab orgs for user {external_user_id}")

    orgs_synced = 0
    workspace_ids: set[str] = set()

    for entry in org_entries:
        external_org_id = entry["external_org_id"]
        try:
            if entry["org_token"]:
                token = app.database.decrypt(entry["org_token"])
                member = await gitlab_plugin.fetch_group_member(
                    token,
                    external_org_id,
                    external_user_id,
                    provider_url=entry["provider_url"],
                )
            elif entry["repo_token"] and entry["repo_external_id"]:
                token = app.database.decrypt(entry["repo_token"])
                member = await gitlab_plugin.fetch_project_member(
                    token,
                    entry["repo_external_id"],
                    external_user_id,
                    provider_url=entry["provider_url"],
                )
            else:
                continue

            if member is None:
                continue

            role = _gitlab_access_level_to_role(member.get("access_level", 10))
            _sync_org_membership(
                app, provider_identity_id, entry["id"], entry["workspace_id"], role
            )
            orgs_synced += 1
            if entry["workspace_id"]:
                workspace_ids.add(str(entry["workspace_id"]))

        except Exception as e:
            logger.warning(f"Failed to check membership for GitLab org {external_org_id}: {e}")
            continue

    logger.info(f"GitLab sync complete: {orgs_synced} orgs")

    for ws_id in workspace_ids:
        await publish_sync_event(
            workspace_id=ws_id,
            action="memberships_synced",
            payload={"provider": "gitlab"},
        )
