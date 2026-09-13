"""Sentry webhook ingestion endpoint (hot path)."""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.webhooks.sentry.dependency import verify_signature

logger = logging.getLogger(__name__)

STREAM_WEBHOOKS = "jeanclode.events.sentry.webhooks"
STREAM_INSTALLATION = "jeanclode.events.sentry.installation"

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/sentry", status_code=200)
async def sentry_webhook(
    body: bytes = Depends(verify_signature),
    sentry_hook_resource: str = Header("", alias="sentry-hook-resource"),
) -> dict[str, str]:
    """Receive a Sentry webhook, verify signature, publish to Redis Stream.

    Routes installation webhooks to a separate stream for onboarding flow.
    All other webhooks go to the main issue processing stream.

    This is the Stage 1 hot path per ADR-004: validate and queue.
    """
    stream = STREAM_INSTALLATION if sentry_hook_resource == "installation" else STREAM_WEBHOOKS

    try:
        broker = get_faststream_broker()
        await broker.publish(body, stream=stream, maxlen=STREAM_MAXLEN)
    except Exception as e:
        logger.exception("Failed to publish webhook to stream {body=%s}", body)
        raise HTTPException(status_code=503, detail="Queue unavailable") from e

    return {"status": "ok"}
