"""Sentry installation webhook consumer.

Consumes ``installation.created`` and ``installation.deleted`` events from the
``jeanclode.events.sentry.installation`` Redis Stream.  Creates an unlinked
Organization (provider=sentry) on install (no workspace yet — the user links
it later via the UI).
"""

import json
import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_create_org,
    db_delete_org,
    db_get_org_by_installation_id,
)

logger = logging.getLogger(__name__)

consumer_router = RedisRouter()


def _parse_payload(raw: bytes) -> dict[str, Any] | None:
    """Parse raw webhook bytes into a dict. Returns None on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Malformed installation webhook payload, discarding")
        return None


@consumer_router.subscriber(
    stream=StreamSub("jeanclode.events.sentry.installation", group="jeanclode", consumer="worker-1")
)
async def consume_sentry_installation(payload: dict[str, Any]) -> None:
    """Consume Sentry installation webhooks from Redis Stream.

    Routes by action:
      - ``created``  → create unlinked Organization (provider=sentry, no workspace yet)
      - ``deleted``  → delete Organization
    """
    action = payload.get("action")
    if action == "created":
        await _handle_installation_created(payload)
    elif action == "deleted":
        await _handle_installation_deleted(payload)
    else:
        logger.debug(f"Ignoring installation action: {action}")


async def _handle_installation_created(payload: dict[str, Any]) -> None:
    """Handle installation.created — create Organization (provider=sentry) without workspace.

    The Organization is linked to a workspace later when the user connects
    Sentry via the UI (POST /sources/sentry).
    """
    installation = payload.get("data", {}).get("installation", {})
    org_data = installation.get("organization", {})
    org_slug = (org_data.get("slug") or "").lower().strip()
    installation_uuid = installation.get("uuid")

    if not org_slug or not installation_uuid:
        logger.warning("Installation webhook missing org slug or uuid, discarding")
        return

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    with db_plugin.session() as db:
        # Check if already exists by installation_id
        existing = db_get_org_by_installation_id(db, installation_uuid)
        if existing:
            logger.info(
                f"Organization for installation {installation_uuid} already exists, skipping"
            )
            return

        # Check if org was created by link endpoint (without installation_id)
        from api.database import db_get_org_by_external_id, db_update_org

        by_slug = db_get_org_by_external_id(db, org_slug, provider="sentry")
        if by_slug and not by_slug.installation_id:
            db_update_org(db, by_slug.id, installation_id=installation_uuid)
            logger.info(f"Updated Organization {org_slug} with installation_id={installation_uuid}")
            return

        # Create new unlinked Organization — user links it later
        db_create_org(
            db,
            workspace_id=None,
            name=org_slug,
            external_org_id=org_slug,
            provider="sentry",
            installation_id=installation_uuid,
            base_url="https://sentry.io",
        )

    logger.info(
        f"Created unlinked Organization (sentry) for {org_slug} (installation={installation_uuid})"
    )


async def _handle_installation_deleted(payload: dict[str, Any]) -> None:
    """Handle installation.deleted — remove Organization by installation UUID."""
    installation = payload.get("data", {}).get("installation", {})
    installation_uuid = installation.get("uuid")

    if not installation_uuid:
        logger.warning("Installation deleted webhook missing uuid, discarding")
        return

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    with db_plugin.session() as db:
        deleted = db_delete_org(db, installation_uuid)
        if deleted:
            logger.info(f"Deleted Organization for installation {installation_uuid}")
        else:
            logger.info(
                f"Organization for installation {installation_uuid} not found "
                "(may have been deleted already)"
            )
