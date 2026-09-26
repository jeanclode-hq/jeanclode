"""Daily memory curation scheduler (ADR-009).

Agents write memory on every run and nothing else prunes it, so once a
workspace has entries changed since the curator last saw them (and at most
once per ``cadence_hours``) this launches the CLI's ``memory-curate``
workflow for it.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any

from api.context import get_current_app
from api.database.memory import db_claim_workspaces_due_for_memory_curation
from api.plugins.container.dispatch_inputs import check_llm_availability
from api.plugins.memory.launch import launch_memory_curation

logger = logging.getLogger(__name__)


class MemoryCurationScheduler:
    """Background loop dispatching the curator for due workspaces."""

    def __init__(self, *, interval_seconds: int, cadence_hours: int, batch_size: int) -> None:
        self._interval_seconds = interval_seconds
        self._cadence = timedelta(hours=cadence_hours)
        self._batch_size = batch_size
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Memory curation scheduler started (interval=%ds, cadence=%s)",
            self._interval_seconds,
            self._cadence,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Memory curation scheduler stopped")

    def health_check(self) -> dict[str, Any]:
        task_alive = self._task is not None and not self._task.done()
        return {"healthy": task_alive, "stopping": self._stopping.is_set()}

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Memory curation tick failed")

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
                break
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        app = get_current_app()
        if not app.database:
            return
        # Checked before claiming so an exhausted pool doesn't burn the workspace's cadence.
        if not (await asyncio.to_thread(check_llm_availability)).available:
            return

        workspace_ids = await app.database.run_in_session(
            lambda db: db_claim_workspaces_due_for_memory_curation(
                db, cadence=self._cadence, limit=self._batch_size
            )
        )

        for workspace_id in workspace_ids:
            await launch_memory_curation(workspace_id)
