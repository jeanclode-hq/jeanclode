"""FastStream consumer for Sentry project sync."""

import logging
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_ensure_org_membership,
    db_get_org_by_id,
    db_get_user_by_email,
    db_upsert_repository,
)
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage
from api.routers.sources.sentry.schemas import (
    SentrySyncMembershipsMessage,
    SentrySyncProjectsMessage,
)
from api.sse.publishers import publish_sync_event

logger = logging.getLogger(__name__)

router = RedisRouter()


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.sentry.sync_projects", group="jeanclode", consumer="worker-1"
    )
)
async def sync_sentry_projects(message: SentrySyncProjectsMessage) -> None:
    """Sync all Sentry projects for an organization.

    Runs in the background after Sentry source setup.
    Fetches all projects from Sentry API using the plugin's config token
    and upserts into DB.  After sync, auto-chains to mapping resolution
    if a git org exists in the same workspace.
    """
    org_id = UUID(message.org_id)
    org_slug = message.org_slug

    logger.info(f"Starting Sentry project sync for org {org_slug}")

    app = get_current_app()
    sentry_plugin = app.sentry
    db_plugin = app.database

    if not db_plugin or not sentry_plugin:
        logger.error("Required plugins not configured")
        return

    # Verify Organization exists and read per-org credentials
    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return
        org_base_url = org.base_url
        org_auth_token_encrypted = org.auth_token_encrypted
        workspace_id = str(org.workspace_id) if org.workspace_id else None

    # Decrypt per-org auth token (falls back to plugin config if not set)
    auth_token = db_plugin.decrypt(org_auth_token_encrypted) if org_auth_token_encrypted else None

    # External API calls — no DB session held
    try:
        projects = await sentry_plugin.list_projects(
            org_slug, auth_token=auth_token, base_url=org_base_url
        )
    except Exception as e:
        logger.error(
            f"Failed to fetch Sentry projects for {org_slug}: {e}",
            exc_info=True,
        )
        return

    logger.info(f"Found {len(projects)} Sentry projects for org {org_slug}")

    # DB writes — upsert each project
    with db_plugin.session() as db:
        for project in projects:
            db_upsert_repository(
                db=db,
                org_id=org_id,
                external_id=project.id,
                name=project.slug,
                provider="sentry",
            )
            logger.debug(f"Upserted repository: {project.slug} ({project.id})")

    logger.info(f"Successfully synced {len(projects)} Sentry projects for org {org_slug}")

    # Publish SSE sync event
    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="projects_synced",
            payload={"org_id": str(org_id), "project_count": len(projects)},
        )

    # Chain membership sync + issue backfill
    try:
        broker = get_faststream_broker()
        await broker.publish(
            SentrySyncMembershipsMessage(
                org_id=str(org_id),
                org_slug=org_slug,
            ),
            stream="jeanclode.events.sentry.sync_memberships",
            maxlen=STREAM_MAXLEN,
        )
        await broker.publish(
            BackfillIssuesMessage(
                org_id=str(org_id),
                org_slug=org_slug,
            ),
            stream="jeanclode.events.sentry.backfill",
            maxlen=STREAM_MAXLEN,
        )
        logger.info(f"Queued membership sync and issue backfill for {org_slug}")
    except Exception as e:
        logger.error(f"Failed to queue post-sync tasks for {org_slug}: {e}")


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.sentry.sync_memberships", group="jeanclode", consumer="worker-1"
    )
)
async def sync_sentry_memberships(message: SentrySyncMembershipsMessage) -> None:
    """Sync Sentry organization memberships by matching emails to existing users.

    Fetches all members from the Sentry org via API, matches their emails
    to existing users' provider identities, and creates OrgMembership records.
    """
    org_id = UUID(message.org_id)
    org_slug = message.org_slug

    logger.info(f"Starting Sentry membership sync for org {org_slug}")

    app = get_current_app()
    sentry_plugin = app.sentry
    db_plugin = app.database

    if not db_plugin or not sentry_plugin:
        logger.error("Required plugins not configured")
        return

    # Read per-org credentials
    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return
        org_base_url = org.base_url
        org_auth_token_encrypted = org.auth_token_encrypted
        workspace_id = str(org.workspace_id) if org.workspace_id else None

    auth_token = db_plugin.decrypt(org_auth_token_encrypted) if org_auth_token_encrypted else None

    # Fetch Sentry org members via API
    try:
        members = await sentry_plugin._request(
            "GET",
            f"/organizations/{org_slug}/users/",
            auth_token=auth_token,
            base_url=org_base_url,
        )
    except Exception as e:
        logger.error(f"Failed to fetch Sentry members for {org_slug}: {e}", exc_info=True)
        return

    if not isinstance(members, list):
        logger.warning(f"Unexpected Sentry members response for {org_slug}: {type(members)}")
        return

    logger.info(f"Found {len(members)} members in Sentry org {org_slug}")

    # Match by email → create memberships
    synced = 0
    with db_plugin.session() as db:
        for member in members:
            email = member.get("email")
            if not email:
                continue

            # Map Sentry role to our role
            org_role = member.get("orgRole", "member")
            if org_role == "owner":
                role = "owner"
            elif org_role in ("admin", "manager"):
                role = "admin"
            else:
                role = "member"

            # Find user by email
            user = db_get_user_by_email(db, email)
            if not user:
                continue

            # Create membership for each of the user's provider identities
            for identity in user.identities:
                db_ensure_org_membership(
                    db,
                    org_id=org_id,
                    provider_identity_id=identity.id,
                    role=role,
                )
            synced += 1

    logger.info(f"Sentry membership sync complete for {org_slug}: {synced} users matched")

    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="memberships_synced",
            payload={"org_id": str(org_id), "synced": synced},
        )
