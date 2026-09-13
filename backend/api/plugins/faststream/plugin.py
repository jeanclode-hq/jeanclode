"""FastStream plugin for asynchronous message processing."""

import asyncio
import logging
from typing import Any

from faststream.redis import RedisBroker
from redis.asyncio import BlockingConnectionPool, Redis

from api.plugins.faststream.config import FastStreamPluginConfig
from api.plugins.plugin import BasePlugin

logger = logging.getLogger(__name__)


class FastStreamPlugin(BasePlugin[FastStreamPluginConfig]):
    """FastStream message queue plugin."""

    plugin_name = "faststream"
    config_class = FastStreamPluginConfig
    priority = 60

    def __init__(self, plugin_config: FastStreamPluginConfig):
        self.config = plugin_config
        self._broker: RedisBroker | None = None
        self._redis: Redis | None = None
        self._additional_routers: list = []
        self._broker_task: asyncio.Task | None = None

    def add_router(self, router) -> None:
        """Add a router to be included when broker is created."""
        self._additional_routers.append(router)

    async def startup(self) -> None:
        """Start the FastStream Redis broker.

        Queue is mandatory once this plugin is enabled: a Redis connectivity
        failure here propagates up (the caller re-raises), crashing the app so
        Kubernetes' own restart backoff applies, instead of degrading silently
        and relying on FastStream's internal no-backoff retry.
        """
        redis_url = self.config.get_redis_url()
        self._broker = RedisBroker(
            url=redis_url,
            graceful_timeout=self.config.graceful_timeout,
        )

        # Every authenticated request reads its session through this client.
        # Blocking, so a burst past max_connections waits for a free connection
        # instead of failing the request with MaxConnectionsError.
        pool: BlockingConnectionPool = BlockingConnectionPool.from_url(
            redis_url,
            max_connections=self.config.redis.max_connections,
            timeout=self.config.redis.pool_timeout,
        )
        self._redis = Redis.from_pool(pool)  # type: ignore[attr-defined]

        # Fail fast if Redis isn't reachable at boot.
        await self._redis.ping()

        # Load consumers from config
        for consumer_config in self.config.consumers:
            try:
                module_path, attr_name = consumer_config.handler.rsplit(":", 1)
                module = __import__(module_path, fromlist=[attr_name])
                router = getattr(module, attr_name)
                self._broker.include_router(router)
            except Exception as e:
                logger.error(
                    f"Failed to load consumer {consumer_config.handler}: {e}",
                    exc_info=True,
                )

        # Include routers added via add_router()
        for additional_router in self._additional_routers:
            self._broker.include_router(additional_router)

        self._broker_task = asyncio.create_task(self._run_broker())

    async def _run_broker(self) -> None:
        """Run the FastStream broker (background task)."""
        if not self._broker:
            return
        try:
            await self._broker.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error running FastStream broker: {e}", exc_info=True)

    async def shutdown(self) -> None:
        """Shutdown the FastStream broker."""
        if self._broker_task and not self._broker_task.done():
            self._broker_task.cancel()
            try:
                await self._broker_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error waiting for broker task cancellation: {e}", exc_info=True)

        if self._broker:
            try:
                await self._broker.stop()
            except Exception as e:
                logger.error(f"Error stopping FastStream broker: {e}", exc_info=True)
            finally:
                self._broker = None
                self._broker_task = None

        if self._redis:
            try:
                await self._redis.aclose()  # type: ignore[attr-defined]
            except Exception as e:
                logger.error(f"Error closing Redis client: {e}")
            finally:
                self._redis = None

    def get_broker(self) -> RedisBroker:
        """Get the broker instance for publishing messages."""
        if not self._broker:
            raise RuntimeError("FastStream broker not initialized")
        return self._broker

    def get_redis(self) -> Redis:
        """Get the Redis client for direct operations."""
        if not self._redis:
            raise RuntimeError("Redis client not initialized")
        return self._redis

    async def publish(self, channel: str, message: Any) -> None:
        """Publish a message to a Redis channel via the broker."""
        broker = self.get_broker()
        await broker.publish(message, channel=channel)

    async def health_check(self) -> dict[str, Any]:
        """Check Redis connectivity by sending a PING."""
        if not self._redis:
            return {"healthy": False, "error": "Redis client not initialized"}
        try:
            await self._redis.ping()
            return {"healthy": True}
        except Exception as e:
            return {"healthy": False, "error": str(e)}
