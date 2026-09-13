"""Redispatch poller for SCHEDULED executions (ADR-010).

The GitHub/GitLab webhook-driven and manual-trigger paths have no existing
poller (unlike Sentry's ADR-006 pending-pool tick), so an execution
admitted as SCHEDULED (every LLM credential exhausted) needs one: a small
periodic task that claims due rows and pushes them back onto whichever
Redis stream they originally came from. The consumer on the other end
(``api.plugins.container.retry_dispatch``) reconstructs the rest of the
dispatch context from the ``Execution`` row itself.

Deliberately separate from :class:`api.plugins.sentry.dispatch.SentryDispatcher`
— it scans a different table (``executions.retry_at`` vs Sentry's
per-issue dispatch window) for a different reason, so sharing one loop
would just tangle two unrelated retry mechanisms together.
"""

import asyncio
import contextlib
import logging
from typing import Any

from api.context import get_current_app
from api.database.execution import db_claim_due_scheduled_executions
from api.models.executions import Execution, ExecutionWorkflow
from api.plugins.faststream import STREAM_MAXLEN

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_BATCH_SIZE = 20


def resolve_retry_stream(execution: Execution) -> str | None:
    """Derive the Redis stream a SCHEDULED execution originally came from.

    Only GitHub/GitLab ever produce a SCHEDULED execution — Sentry's own
    30-second tick already covers its retry need (ADR-010), so a `sentry`
    execution here would indicate a bug rather than a real case to handle.
    """
    if execution.provider not in ("github", "gitlab"):
        logger.warning(
            "Scheduled execution %s has unexpected provider=%s — skipping redispatch",
            execution.id,
            execution.provider,
        )
        return None

    suffix = (
        "issue_resolve"
        if execution.workflow == ExecutionWorkflow.ISSUE_RESOLVE.value
        else ("manual_dispatch")
    )
    return f"jeanclode.events.{execution.provider}.{suffix}"


class ScheduledExecutionPoller:
    """Background loop that redispatches SCHEDULED executions once due."""

    def __init__(
        self,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Scheduled-execution poller started (interval=%ds, batch=%d)",
            self._interval_seconds,
            self._batch_size,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Scheduled-execution poller stopped")

    def health_check(self) -> dict[str, Any]:
        task_alive = self._task is not None and not self._task.done()
        return {"healthy": task_alive, "stopping": self._stopping.is_set()}

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Scheduled-execution redispatch tick failed")

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
                break
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        app = get_current_app()
        db_plugin = app.database
        faststream = app.faststream
        if not db_plugin or not faststream:
            return

        with db_plugin.session() as db:
            claimed = db_claim_due_scheduled_executions(db, self._batch_size)

        if not claimed:
            return

        broker = faststream.get_broker()
        for execution in claimed:
            stream = resolve_retry_stream(execution)
            if not stream:
                continue
            await broker.publish(
                {"execution_id": str(execution.id)},
                stream=stream,
                maxlen=STREAM_MAXLEN,
            )
            logger.info(
                "Redispatched execution %s (provider=%s, workflow=%s) -> %s",
                str(execution.id)[:8],
                execution.provider,
                execution.workflow,
                stream,
            )
