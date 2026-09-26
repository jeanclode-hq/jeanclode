"""Memory curation plugin configuration."""

from pydantic import BaseModel, Field


class MemoryWatcherConfig(BaseModel):
    """Configuration for the memory curator's container watcher."""

    enabled: bool = Field(default=True, description="Enable container watching")
    reconcile_interval: int = Field(default=60, description="Seconds between reconciliation scans")


class MemoryCurationConfig(BaseModel):
    """When the curator runs."""

    enabled: bool = Field(default=True, description="Run the curation scheduler in this process")
    interval_seconds: int = Field(
        default=900, description="Seconds between scans for due workspaces"
    )
    cadence_hours: int = Field(
        default=24, description="Minimum hours between two curator runs on one workspace"
    )
    batch_size: int = Field(default=5, description="Max workspaces dispatched per scan")
    timeout_seconds: int = Field(
        default=3600,
        description="Container timeout for one curator run, which curates every due entry",
    )


class MemoryPluginConfig(BaseModel):
    """Memory curation plugin configuration."""

    enabled: bool = True
    watcher: MemoryWatcherConfig = Field(default_factory=MemoryWatcherConfig)
    curation: MemoryCurationConfig = Field(default_factory=MemoryCurationConfig)
