"""Manual trigger endpoint for Sentry issue backfill."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from api.context import get_current_app
from api.database import db_get_org_by_id
from api.models import User
from api.models.settings import BackfillScope, SentryOrgSettings
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.auth.dependencies import verify_org_access

from .schemas import BackfillIssuesMessage, BackfillRequest, BackfillResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backfill", tags=["Sentry Backfill"])

# Used when a manual run is requested but the org has backfill switched off —
# the click is the intent, so import something useful rather than nothing.
MANUAL_FALLBACK_SCOPE = BackfillScope.DAYS_30


def _resolve_requested_scope(
    requested: BackfillScope | None, org_settings: dict | None
) -> BackfillScope:
    """Pick the scope for a manually triggered run.

    An explicit request wins. Otherwise the org's configured scope applies,
    except when that scope is ``none``: the user asked for an import, so
    fall back to the default window instead of queueing a no-op.
    """
    if requested is not None and requested is not BackfillScope.NONE:
        return requested

    try:
        configured = SentryOrgSettings.model_validate(org_settings or {}).backfill
    except Exception:
        logger.warning("Invalid Sentry org settings, using fallback scope for manual backfill")
        return MANUAL_FALLBACK_SCOPE

    return MANUAL_FALLBACK_SCOPE if configured is BackfillScope.NONE else configured


@router.post(
    "",
    operation_id="trigger_backfill",
    response_model=BackfillResponse,
    status_code=202,
)
async def trigger_backfill(
    request: BackfillRequest | None = None,
    org_id: uuid.UUID = Query(..., description="Organization ID"),
    current_user: User = Depends(verify_org_access),
) -> BackfillResponse:
    """Manually trigger issue backfill for a Sentry org.

    Queues a background task that fetches unresolved issues from the Sentry
    API for each mapped project and creates Issue records with
    status=PENDING.

    The run carries an explicit scope, so it proceeds even for an org whose
    stored preference is ``none`` — skipping the import at onboarding is a
    deferral, not a permanent opt-out.
    """
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        raise HTTPException(status_code=503, detail="Database not configured")

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        org_slug = org.external_org_id
        org_settings = org.settings

    scope = _resolve_requested_scope(request.scope if request else None, org_settings)

    broker = get_faststream_broker()
    await broker.publish(
        BackfillIssuesMessage(
            org_id=str(org_id),
            org_slug=org_slug,
            scope=scope,
        ),
        stream="jeanclode.events.sentry.backfill",
        maxlen=STREAM_MAXLEN,
    )

    return BackfillResponse(status="accepted", scope=scope)
