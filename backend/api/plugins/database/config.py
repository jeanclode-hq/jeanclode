"""Database plugin configuration."""

from pydantic import BaseModel, Field


class DatabasePluginConfig(BaseModel):
    """Database plugin configuration."""

    enabled: bool = False
    url: str
    echo: bool = False
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    # Fail a checkout fast rather than freezing (a sync query on the event
    # loop that waits here stalls the whole process — long enough to trip
    # the liveness probe). A 500 on one request beats a pod restart.
    pool_timeout: float = Field(default=5.0, gt=0)
    encryption_key: str | None = None
