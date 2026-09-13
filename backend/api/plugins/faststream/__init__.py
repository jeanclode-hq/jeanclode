"""FastStream plugin for message queue operations."""

from fastapi import HTTPException
from faststream.redis import RedisBroker

from api.context import get_current_app

STREAM_MAXLEN = 10000


def get_faststream_broker() -> RedisBroker:
    """Get FastStream broker instance.

    Returns:
        RedisBroker instance

    Raises:
        HTTPException: 503 if FastStream plugin not enabled or broker not initialized
    """
    app = get_current_app()

    if not app.faststream:
        raise HTTPException(
            status_code=503,
            detail="Queue service is not configured. Please enable FastStream plugin.",
        )

    try:
        return app.faststream.get_broker()
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail="Queue service is not available. Please try again later.",
        ) from e
