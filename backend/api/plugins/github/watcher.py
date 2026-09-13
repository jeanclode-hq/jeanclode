"""GitHub container watcher.

Thin wrapper around a ContainerBackend (Docker or Kubernetes) scoped to
"github". All watching infrastructure (events, reconciliation, log
streaming) lives in the container plugin — this module just configures
and starts it.
"""

import asyncio
import contextlib
import logging

from api.plugins.container.backend import ContainerBackend
from api.plugins.github.config import GitHubWatcherConfig

logger = logging.getLogger(__name__)


class GitHubWatcher:
    """GitHub-specific container watcher.

    Wraps a ContainerBackend (Docker or Kubernetes) scoped to
    plugin="github" and manages the watching + reconciliation lifecycle.
    """

    def __init__(
        self,
        backend: ContainerBackend,
        config: GitHubWatcherConfig,
    ) -> None:
        self._backend = backend
        self._config = config
        self._reconcile_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start watching containers and reconciliation loop."""
        await self._backend.start_watching()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info(
            "GitHub watcher started (reconcile_interval=%ds)",
            self._config.reconcile_interval,
        )

    async def stop(self) -> None:
        """Stop watching and reconciliation."""
        if self._reconcile_task:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

        await self._backend.stop_watching()
        logger.info("GitHub watcher stopped")

    async def _reconcile_loop(self) -> None:
        """Periodically reconcile with distributed locking."""
        while True:
            try:
                await asyncio.sleep(self._config.reconcile_interval)

                if not await self._backend.acquire_reconcile_lock():
                    logger.debug("Skipping reconciliation - lock held by another instance")
                    continue

                try:
                    await self._backend.reconcile()
                finally:
                    await self._backend.release_reconcile_lock()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in github reconciliation loop: {e}")

    @property
    def backend(self) -> ContainerBackend:
        """Get the underlying container backend."""
        return self._backend

    def health_check(self) -> dict:
        """Check watcher health."""
        return {
            "healthy": self._reconcile_task is not None and not self._reconcile_task.done(),
        }
