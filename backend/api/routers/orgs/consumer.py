"""FastStream consumer for org member pre-population.

When an admin connects a GitHub org or GitLab group, this job fetches all
current members and creates ProviderIdentity rows (user_id=NULL) + OrgMembership
rows so teammates are known before they log in for the first time.
"""

import logging
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import db_get_org_by_id
from api.database.identity import db_get_or_create_provider_identity
from api.models.organizations import OrgMembership
from api.routers.orgs.schemas import SyncOrgMembersMessage

logger = logging.getLogger(__name__)

router = RedisRouter()


def _upsert_org_membership(db, org_id: UUID, provider_identity_id: UUID, role: str) -> None:
    """Create or update an OrgMembership row."""
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
        db.add(OrgMembership(org_id=org_id, provider_identity_id=provider_identity_id, role=role))
    db.commit()


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.orgs.sync_members",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def sync_org_members(message: SyncOrgMembersMessage) -> None:
    """Pre-populate ProviderIdentity + OrgMembership for all org members.

    Runs in the background after an admin connects an org. Members who haven't
    logged in yet get ProviderIdentity rows with user_id=NULL; their
    WorkspaceMembership will be created when they first log in.
    """
    org_id = UUID(message.org_id)
    provider = message.provider

    logger.info(f"Starting member sync for org {org_id} ({provider})")

    app = get_current_app()
    db_plugin = app.database

    if not db_plugin:
        logger.error("Database plugin not available")
        return

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return

        # Placeholder ancestor orgs (created for hierarchy only) have no token — skip them.
        # GitHub orgs use on-demand installation tokens, so they never have auth_token_encrypted.
        if provider != "github" and not org.auth_token_encrypted:
            logger.debug(f"Skipping member sync for placeholder org {org_id} (no token)")
            return

        external_org_id = org.external_org_id
        installation_id = org.installation_id
        auth_token_encrypted = org.auth_token_encrypted
        provider_url = org.base_url or "https://gitlab.com"
        org_name = org.name

    if provider == "github":
        await _sync_github_members(app, org_id, org_name, installation_id, external_org_id)
    elif provider == "gitlab":
        await _sync_gitlab_members(app, org_id, external_org_id, auth_token_encrypted, provider_url)
    else:
        logger.warning(f"Unknown provider '{provider}' for org {org_id}")


async def _sync_github_members(
    app,
    org_id: UUID,
    org_name: str,
    installation_id: str | None,
    external_org_id: str,
) -> None:
    """Fetch all GitHub org members and upsert ProviderIdentity + OrgMembership rows."""
    github_plugin = app.github
    db_plugin = app.database

    if not github_plugin:
        logger.warning("GitHub plugin not available for member sync")
        return

    if not installation_id:
        logger.warning(f"No installation_id for org {org_id}, cannot fetch members")
        return

    try:
        installation_token = await github_plugin.get_installation_access_token(installation_id)
    except Exception as e:
        logger.error(f"Failed to get installation token for {installation_id}: {e}")
        return

    headers = {
        "Authorization": f"token {installation_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    members: list[dict] = []
    page = 1
    while True:
        try:
            response = await github_plugin.http.get(
                f"/orgs/{org_name}/members",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
        except Exception as e:
            logger.error(f"Failed to fetch GitHub org members page {page}: {e}")
            break

        if response.status_code != 200:
            logger.error(f"GitHub /orgs/{org_name}/members returned {response.status_code}")
            break

        page_members = response.json()
        if not page_members:
            break

        members.extend(page_members)
        page += 1

    logger.info(f"Found {len(members)} members for GitHub org {org_name}")

    if not db_plugin:
        return

    synced = 0
    for member in members:
        external_id = str(member["id"])
        username = member.get("login")
        avatar_url = member.get("avatar_url")

        try:
            with db_plugin.session() as db:
                identity, _ = db_get_or_create_provider_identity(
                    db=db,
                    provider="github",
                    external_id=external_id,
                    username=username,
                    avatar_url=avatar_url,
                )
                _upsert_org_membership(db, org_id, identity.id, "member")
                synced += 1
        except Exception as e:
            logger.warning(f"Failed to upsert GitHub member {username} ({external_id}): {e}")

    logger.info(f"GitHub member sync complete: {synced}/{len(members)} members for org {org_id}")


async def _sync_gitlab_members(
    app,
    org_id: UUID,
    group_id: str,
    auth_token_encrypted: str | None,
    provider_url: str,
) -> None:
    """Fetch all GitLab group members and upsert ProviderIdentity + OrgMembership rows."""
    gitlab_plugin = app.gitlab
    db_plugin = app.database

    if not gitlab_plugin:
        logger.warning("GitLab plugin not available for member sync")
        return

    if not auth_token_encrypted or not db_plugin:
        logger.warning(f"No auth token for GitLab group {group_id}, cannot fetch members")
        return

    access_token = db_plugin.decrypt(auth_token_encrypted)

    members: list[dict] = []
    page = 1
    while True:
        try:
            response = await gitlab_plugin.http.get(
                f"{provider_url}/api/v4/groups/{group_id}/members",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"per_page": 100, "page": page},
            )
        except Exception as e:
            logger.error(f"Failed to fetch GitLab group members page {page}: {e}")
            break

        if response.status_code != 200:
            logger.error(f"GitLab /groups/{group_id}/members returned {response.status_code}")
            break

        page_members = response.json()
        if not page_members:
            break

        members.extend(page_members)
        page += 1

    logger.info(f"Found {len(members)} members for GitLab group {group_id}")

    synced = 0
    for member in members:
        external_id = str(member["id"])
        username = member.get("username")
        avatar_url = member.get("avatar_url")

        access_level = member.get("access_level", 10)
        if access_level >= 50:
            role = "owner"
        elif access_level >= 40:
            role = "admin"
        else:
            role = "member"

        try:
            with db_plugin.session() as db:
                identity, _ = db_get_or_create_provider_identity(
                    db=db,
                    provider="gitlab",
                    external_id=external_id,
                    username=username,
                    avatar_url=avatar_url,
                )
                _upsert_org_membership(db, org_id, identity.id, role)
                synced += 1
        except Exception as e:
            logger.warning(f"Failed to upsert GitLab member {username} ({external_id}): {e}")

    logger.info(f"GitLab member sync complete: {synced}/{len(members)} members for org {org_id}")
