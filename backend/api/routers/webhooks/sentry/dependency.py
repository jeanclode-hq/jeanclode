"""Sentry webhook dependencies."""

import hashlib
import hmac
import json
import logging

from fastapi import Header, HTTPException, Request

from api.context import get_current_app
from api.database.organization import db_get_org_by_installation_id
from api.models.organizations import Organization

logger = logging.getLogger(__name__)


def _check_signature(body: bytes, secret: str, expected: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, expected)


async def verify_signature(
    request: Request,
    sentry_hook_signature: str = Header(..., alias="sentry-hook-signature"),
) -> bytes:
    """Verify Sentry HMAC-SHA256 signature and return the raw body.

    Lookup order:
    1. By installation.uuid → fast exact match
    2. Try all orgs with a client_secret → brute-force HMAC match
       (handles the case where installation_id isn't stored yet)

    If a match is found via brute-force, the installation_id is stored
    on the org for future fast lookups.
    """
    body = await request.body()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="Invalid payload") from err

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        raise HTTPException(status_code=500, detail="Database not configured")

    installation_uuid = payload.get("installation", {}).get("uuid")

    # 1. Fast path: look up by installation_id
    if installation_uuid:
        with db_plugin.session() as db:
            org = db_get_org_by_installation_id(db, installation_uuid)
            if org and org.client_secret_encrypted:
                secret = db_plugin.decrypt(org.client_secret_encrypted)
                if _check_signature(body, secret, sentry_hook_signature):
                    return body

    # 2. Slow path: try all orgs with a client_secret
    if installation_uuid:
        with db_plugin.session() as db:
            orgs = (
                db.query(Organization)
                .filter(
                    Organization.provider == "sentry",
                    Organization.client_secret_encrypted.isnot(None),
                )
                .limit(100)
                .all()
            )
            for org in orgs:
                secret = db_plugin.decrypt(org.client_secret_encrypted)
                if _check_signature(body, secret, sentry_hook_signature):
                    # Match found — store installation_id for future fast lookups
                    if not org.installation_id:
                        org.installation_id = installation_uuid
                        db.commit()
                        logger.info(
                            f"Auto-linked installation {installation_uuid} to Organization {org.external_org_id}"
                        )
                    return body

    logger.error("No matching Sentry org found for webhook signature")
    raise HTTPException(status_code=401, detail="Invalid signature")
