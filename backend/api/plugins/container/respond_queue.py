"""Drains the respond queue once the RESPOND execution ahead of it finishes.

Respond dispatch (``api.routers.webhooks.*.mentions``/``notes``) admits a
second mention on the same PR/issue as a QUEUED execution instead of
dropping it, but deliberately never publishes it — nothing would be
watching that stream entry until the one ahead of it clears, and running
both at once is the exact bug this queue exists to avoid.

Every place a RESPOND execution can reach a terminal status must call
:func:`dispatch_next_queued_respond` so the wait doesn't become permanent:

* the container-status consumer, for normal completion/failure — this also
  covers restart-replay and all three reconcile phases, since they all
  funnel through the same ``publish_status``/``publish_status_idempotent``
  call into that consumer.
* ``cancel_execution`` (both the SCHEDULED and RUNNING branches) — the one
  path that deliberately bypasses the status stream so a stray container
  completion can't clobber the cancellation.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import selectinload

from api.context import get_current_app
from api.database.execution import db_get_next_queued_respond_execution
from api.models.executions import Execution, ExecutionWorkflow
from api.plugins.container.retry_dispatch import resolve_retry_context
from api.plugins.faststream import STREAM_MAXLEN

logger = logging.getLogger(__name__)


async def dispatch_next_queued_respond(execution_id: uuid.UUID) -> None:
    """Publish the oldest respond queued behind ``execution_id``, if any.

    ``execution_id`` is the RESPOND execution that just reached a terminal
    status. A no-op for any other workflow, or when nothing is waiting.
    """
    app = get_current_app()
    db_plugin = app.database
    faststream = app.faststream
    if not db_plugin or not faststream:
        return

    with db_plugin.session() as db:
        execution = (
            db.query(Execution)
            .options(selectinload(Execution.pull_requests), selectinload(Execution.issues))
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution or execution.workflow != ExecutionWorkflow.RESPOND.value:
            return

        pr_id = execution.pull_requests[0].id if execution.pull_requests else None
        issue_id = execution.issues[0].id if execution.issues else None
        if pr_id is None and issue_id is None:
            return

        next_execution = db_get_next_queued_respond_execution(
            db, pull_request_id=pr_id, issue_id=issue_id
        )
        if not next_execution:
            return

        provider = next_execution.provider
        next_id = next_execution.id
        retry_context = resolve_retry_context(db, next_id)

    if retry_context is None or retry_context.organization_id is None:
        logger.warning(
            "Queued respond execution %s has no resolvable target — not dispatching",
            next_id,
        )
        return

    await faststream.get_broker().publish(
        {
            "execution_id": str(next_id),
            "organization_id": retry_context.organization_id,
            "workflow": ExecutionWorkflow.RESPOND.value,
            "pull_request_id": retry_context.pull_request_id,
            "issue_id": retry_context.issue_id,
            "target_url": retry_context.target_url,
        },
        stream=f"jeanclode.events.{provider}.manual_dispatch",
        maxlen=STREAM_MAXLEN,
    )
    logger.info(
        "Dispatched queued respond execution %s (was waiting behind %s)",
        str(next_id)[:8],
        str(execution_id)[:8],
    )
