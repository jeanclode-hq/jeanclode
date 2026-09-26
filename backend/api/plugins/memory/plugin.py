"""Memory curation plugin.

Owns everything about the daily memory curator: its container backend
(scoped ``plugin="memory"``, so executions carry ``provider="memory"``),
the watcher and reconciler for those containers, the status consumer, and
the scheduler that dispatches a run per due workspace.
"""

import logging
from typing import Any

from api.context import get_current_app
from api.plugins.container.backend import ContainerBackend
from api.plugins.container.docker import DockerBackend
from api.plugins.container.kubernetes import KubernetesBackend
from api.plugins.container.plugin import ContainerPlugin
from api.plugins.faststream.plugin import FastStreamPlugin
from api.plugins.memory.config import MemoryPluginConfig
from api.plugins.memory.consumer import router
from api.plugins.memory.dispatch import MemoryCurationScheduler
from api.plugins.memory.launch import PLUGIN_NAME
from api.plugins.memory.watcher import MemoryWatcher
from api.plugins.plugin import BasePlugin

logger = logging.getLogger(__name__)


class MemoryPlugin(BasePlugin[MemoryPluginConfig]):
    """Periodic curation of each workspace's agent memory."""

    plugin_name = PLUGIN_NAME
    config_class = MemoryPluginConfig
    # Before FastStream (60), which includes registered routers when it starts.
    priority = 55

    def __init__(self, config: MemoryPluginConfig) -> None:
        self.config = config
        self._watcher: MemoryWatcher | None = None
        self._scheduler: MemoryCurationScheduler | None = None

    @property
    def watcher(self) -> MemoryWatcher | None:
        return self._watcher

    async def startup(self) -> None:
        app = get_current_app()
        if app.faststream:
            app.faststream.add_router(router)

    async def watch(self) -> None:
        """Start the watcher, then the scheduler that feeds it."""
        app = get_current_app()
        if not app.container or not app.faststream or not app.database:
            logger.warning("Container, FastStream or Database plugin missing, skipping memory")
            return

        if self.config.watcher.enabled:
            backend = self._build_backend(app.container, app.faststream)
            if backend is None:
                return
            self._watcher = MemoryWatcher(backend, self.config.watcher)
            await self._watcher.start()

        curation = self.config.curation
        if curation.enabled:
            self._scheduler = MemoryCurationScheduler(
                interval_seconds=curation.interval_seconds,
                cadence_hours=curation.cadence_hours,
                batch_size=curation.batch_size,
            )
            await self._scheduler.start()

    def _build_backend(
        self, container: ContainerPlugin, faststream: FastStreamPlugin
    ) -> ContainerBackend | None:
        broker = faststream.get_broker()
        redis = faststream.get_redis()
        if container.config.backend == "kubernetes":
            k8s_config = container.config.kubernetes
            if not k8s_config:
                logger.warning("Kubernetes config not available, skipping memory watcher")
                return None
            return KubernetesBackend(PLUGIN_NAME, k8s_config, broker, redis)
        docker_config = container.config.docker
        if not docker_config:
            logger.warning("Docker config not available, skipping memory watcher")
            return None
        return DockerBackend(PLUGIN_NAME, docker_config, broker, redis)

    async def shutdown(self) -> None:
        if self._scheduler:
            await self._scheduler.stop()
            self._scheduler = None
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None

    async def health_check(self) -> dict[str, Any]:
        result: dict[str, Any] = {"healthy": True}
        for name, part in (("watcher", self._watcher), ("scheduler", self._scheduler)):
            if part:
                result[name] = part.health_check()
                if not result[name]["healthy"]:
                    result["healthy"] = False
        return result
