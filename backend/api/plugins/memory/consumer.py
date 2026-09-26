"""FastStream consumer for the memory curator's container status updates."""

import logging
import uuid
from typing import Any

from faststream.redis import RedisRouter, StreamSub
from pydantic import BaseModel

from api.context import get_current_app
from api.database.execution import db_update_execution_status
from api.models.executions import ExecutionStatus
from api.plugins.container.consumer_gate import status_consumer_gate

logger = logging.getLogger(__name__)

router = RedisRouter()


class ExecutionStatusMessage(BaseModel):
    """Status update from the container watcher."""

    execution_id: str
    status: str
    container_id: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    error_type: str | None = None
    result: dict[str, Any] | None = None
    retry_at: str | None = None
    timestamp: str | None = None


@router.subscriber(
    stream=StreamSub(
        "jeanclode.memory.execution.status",
        group="jeanclode",
        consumer="worker-1",
    )
)
async def consume_execution_status(message: ExecutionStatusMessage) -> None:
    """Record a curator run's status; a mid-run rate limit ends it as failed."""
    async with status_consumer_gate:
        await handle_execution_status(message)


async def handle_execution_status(message: ExecutionStatusMessage) -> None:
    db_plugin = get_current_app().database
    if not db_plugin:
        return

    status, error_type, error_detail = message.status, message.error_type, message.error_message
    if status == ExecutionStatus.SCHEDULED.value:
        # Nothing to redispatch: the entries it didn't finish stay due for the next scan.
        status = ExecutionStatus.FAILED.value
        error_type = error_type or "rate_limited"
        error_detail = error_detail or "Rate limited mid-run; retried by the next curation scan"

    found = await db_plugin.run_in_session(
        lambda db: (
            db_update_execution_status(
                db,
                uuid.UUID(message.execution_id),
                status,
                error_type=error_type,
                error_detail=error_detail,
            )
            is not None
        )
    )
    if not found:
        logger.warning("Memory curation execution %s not found", message.execution_id)
        return
    logger.info("Memory curation %s → %s", message.execution_id[:8], status)
