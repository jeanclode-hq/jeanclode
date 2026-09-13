"""Container plugin — config holder for container execution.

The container plugin holds configuration (Docker/K8s settings, watcher config)
but does NOT start any watchers. Domain plugins (e.g., sentry) create and
start their own watchers via the watch() lifecycle hook.
"""

import logging
from typing import Any

from api.context import get_current_app
from api.plugins.container.config import ContainerPluginConfig
from api.plugins.container.scheduled_dispatch import ScheduledExecutionPoller
from api.plugins.plugin import BasePlugin

logger = logging.getLogger(__name__)


class ContainerPlugin(BasePlugin[ContainerPluginConfig]):
    """Plugin for container execution configuration.

    Holds Docker/Kubernetes config used by domain plugins to create
    their own scoped backends and watchers. Also owns the SCHEDULED-execution
    redispatch poller (ADR-010) — provider-agnostic (spans both GitHub and
    GitLab executions), so it lives here rather than in a domain plugin.
    """

    plugin_name = "container"
    config_class = ContainerPluginConfig
    priority = 70

    def __init__(self, config: ContainerPluginConfig) -> None:
        self.config = config
        self._scheduled_dispatch: ScheduledExecutionPoller | None = None

    async def startup(self) -> None:
        """Start the container plugin."""
        logger.info("Container plugin started (backend=%s)", self.config.backend)

    async def watch(self) -> None:
        """Start the redispatch poller once Database + FastStream are ready."""
        dispatch_config = self.config.scheduled_dispatch
        if not dispatch_config.enabled:
            return

        app = get_current_app()
        if not app.database or not app.faststream:
            logger.warning(
                "Database or FastStream plugin not available, skipping scheduled-dispatch poller"
            )
            return

        self._scheduled_dispatch = ScheduledExecutionPoller(
            interval_seconds=dispatch_config.interval_seconds,
            batch_size=dispatch_config.batch_size,
        )
        await self._scheduled_dispatch.start()

    async def shutdown(self) -> None:
        """Stop the container plugin."""
        if self._scheduled_dispatch:
            await self._scheduled_dispatch.stop()
            self._scheduled_dispatch = None
        logger.info("Container plugin stopped")

    async def health_check(self) -> dict[str, Any]:
        """Check container plugin health."""
        result: dict[str, Any] = {"healthy": True, "backend": self.config.backend}
        if self._scheduled_dispatch:
            result["scheduled_dispatch"] = self._scheduled_dispatch.health_check()
            if not result["scheduled_dispatch"]["healthy"]:
                result["healthy"] = False
        return result
