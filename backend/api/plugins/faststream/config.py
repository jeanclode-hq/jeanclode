"""FastStream plugin configuration."""

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class FastStreamConsumerConfig(BaseModel):
    """Configuration for a single FastStream consumer."""

    topic: str = Field(description="Topic/channel to subscribe to")
    group_id: str | None = Field(default=None, description="Consumer group ID (optional)")
    handler: str = Field(description="Module path to handler router")


class FastStreamRedisConfig(BaseModel):
    """FastStream Redis broker configuration."""

    url: str = Field(default="redis://localhost:6379", description="Redis connection URL")
    host: str | None = Field(default=None, description="Redis host (alternative to URL)")
    port: int | None = Field(default=None, description="Redis port (alternative to URL)")
    db: int = Field(default=0, description="Redis database number")
    list_cap_size: int = Field(default=10000, description="Maximum size of Redis lists")
    auto_offset_reset: Literal["latest", "earliest"] = Field(default="latest")
    batch_size: int = Field(default=1, description="Number of messages to fetch in one batch")
    polling_interval: float = Field(default=0.1, description="Polling interval in seconds")
    max_retries: int = Field(default=3, description="Maximum retries for message processing")
    retry_delay: float = Field(default=1.0, description="Delay between retries in seconds")
    max_connections: int = Field(
        default=100, description="Connections in the shared client pool (sessions, locks)"
    )
    pool_timeout: float = Field(
        default=5.0, description="Seconds to wait for a free pooled connection before failing"
    )


class FastStreamPluginConfig(BaseModel):
    """FastStream plugin configuration."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=False, description="Whether FastStream queue is enabled")
    redis: FastStreamRedisConfig = Field(default_factory=FastStreamRedisConfig)
    app_name: str = Field(default="jeanclode-queue", description="FastStream application name")
    graceful_timeout: float = Field(
        default=30.0, description="Graceful shutdown timeout in seconds"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    consumers: list[FastStreamConsumerConfig] = Field(default_factory=list)

    def get_redis_url(self) -> str:
        """Build Redis URL from configuration."""
        if self.redis.url:
            parsed = urlparse(self.redis.url)
            if self.redis.db and parsed.path in ("", "/"):
                return f"{self.redis.url.rstrip('/')}/{self.redis.db}"
            return self.redis.url

        if self.redis.host and self.redis.port:
            return f"redis://{self.redis.host}:{self.redis.port}/{self.redis.db}"

        return "redis://localhost:6379/0"
