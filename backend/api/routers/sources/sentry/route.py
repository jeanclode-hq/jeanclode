"""Sentry sources endpoint — link a Sentry org to a workspace."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.context import get_current_app
from api.database import (
    db_create_org,
    db_get_org_by_external_id,
    db_get_workspace_by_id,
    db_update_org,
)
from api.database.workspace import db_is_workspace_member
from api.models import User
from api.models.settings import BackfillScope, SentryOrgSettings
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.auth.dependencies import get_current_user

from .schemas import (
    LinkSentrySourceRequest,
    SentrySourceResponse,
    SentrySyncProjectsMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["Sources"])


def _settings_with_backfill(current: dict | None, scope: BackfillScope | None) -> dict:
    """Return the org's settings with the backfill scope applied.

    Preserves any other stored settings (trigger config, say) so relinking an
    org doesn't reset them. A ``None`` scope leaves the stored scope alone —
    re-linking to rotate a token shouldn't silently re-enable an import the
    tenant had switched off.
    """
    settings = SentryOrgSettings.model_validate(current or {})
    if scope is not None:
        settings.backfill = scope
    return settings.model_dump(mode="json")


@router.post(
    "/sentry",
    operation_id="link_sentry_source",
    response_model=SentrySourceResponse,
)
async def link_sentry_source(
    request: LinkSentrySourceRequest,
    current_user: User = Depends(get_current_user),
) -> SentrySourceResponse:
    """Link a Sentry organization to a workspace.

    The Organization (provider=sentry) is created automatically when Sentry
    sends an ``installation.created`` webhook.  This endpoint links it to a
    workspace so that project sync and issue processing can begin.

    Flow:
    1. Calls Sentry API with the provided auth_token to discover the org slug
    2. Finds the webhook-created Organization by slug (or creates one)
    3. Encrypts and stores the auth_token on the Organization
    4. Links it to the provided workspace
    5. Stores the requested backfill scope, which bounds how much existing
       Sentry history the chained backfill imports
    6. Queues background project sync
    """
    app = get_current_app()
    sentry_plugin = app.sentry

    if not sentry_plugin:
        raise HTTPException(status_code=404, detail="Sentry integration not configured")

    db_plugin = app.database
    if not db_plugin:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Verify workspace exists and user has access
    with db_plugin.session() as db:
        workspace = db_get_workspace_by_id(db, request.workspace_id)
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if not db_is_workspace_member(db, request.workspace_id, current_user.id):
            raise HTTPException(status_code=403, detail="You don't have access to this workspace")

    # Validate the token by fetching the org directly
    org_slug = request.org_slug.lower().strip()
    try:
        await sentry_plugin.get_organization(
            org_slug, auth_token=request.auth_token, base_url=request.base_url
        )
    except Exception as e:
        logger.error(f"Failed to validate Sentry org '{org_slug}': {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Could not access Sentry org '{org_slug}'. Check your token and org slug.",
        ) from e

    # Encrypt credentials for storage
    auth_token_encrypted = db_plugin.encrypt(request.auth_token)
    client_secret_encrypted = db_plugin.encrypt(request.client_secret)

    # Find or create the Organization (provider=sentry).
    # The installation.created webhook may have already created it with the installation_id.
    # We look up by slug first. For self-hosted uniqueness, base_url is checked too.
    with db_plugin.session() as db:
        org = db_get_org_by_external_id(db, org_slug, provider="sentry")

        if org and org.workspace_id is not None:
            if org.workspace_id != request.workspace_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"Sentry org '{org_slug}' is already linked to another workspace",
                )
            # Already linked — update credentials idempotently
            db_update_org(
                db,
                org.id,
                auth_token_encrypted=auth_token_encrypted,
                client_secret_encrypted=client_secret_encrypted,
                base_url=request.base_url,
                settings=_settings_with_backfill(org.settings, request.backfill_scope),
            )
            org_id = str(org.id)
        elif org:
            # Exists (from webhook) but not linked — link to workspace and store credentials
            db_update_org(
                db,
                org.id,
                workspace_id=request.workspace_id,
                auth_token_encrypted=auth_token_encrypted,
                client_secret_encrypted=client_secret_encrypted,
                base_url=request.base_url,
                settings=_settings_with_backfill(org.settings, request.backfill_scope),
            )
            org_id = str(org.id)
            logger.info(
                f"Linked Organization (sentry) {org_slug} to workspace {request.workspace_id}"
            )
        else:
            # Create new Organization — installation_id will be set when webhook arrives
            org = db_create_org(
                db,
                workspace_id=request.workspace_id,
                name=org_slug,
                external_org_id=org_slug,
                provider="sentry",
                auth_token_encrypted=auth_token_encrypted,
                client_secret_encrypted=client_secret_encrypted,
                base_url=request.base_url,
                settings=_settings_with_backfill(None, request.backfill_scope),
            )
            org_id = str(org.id)
            logger.info(
                f"Created Organization (sentry) {org_slug} linked to workspace {request.workspace_id}"
            )

    # Queue background project sync (fetches project list for the UI)
    projects_synced = False
    try:
        broker = get_faststream_broker()
        await broker.publish(
            SentrySyncProjectsMessage(
                org_id=org_id,
                org_slug=org_slug,
            ),
            stream="jeanclode.events.sentry.sync_projects",
            maxlen=STREAM_MAXLEN,
        )
        projects_synced = True
    except Exception as e:
        logger.error(f"Failed to queue project sync for {org_slug}: {e}")

    return SentrySourceResponse(
        org_id=org_id,
        org_slug=org_slug,
        projects_synced=projects_synced,
    )
