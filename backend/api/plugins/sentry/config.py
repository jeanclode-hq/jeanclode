"""Sentry plugin configuration."""

from pydantic import BaseModel, Field


class SentryDispatchConfig(BaseModel):
    """Configuration for the Sentry issue dispatch loop."""

    enabled: bool = Field(default=False, description="Enable the dispatch loop")
    interval_seconds: int = Field(default=30, description="Seconds between dispatch ticks")
    batch_size: int = Field(default=5, description="Max issues per dispatch batch")
    max_retries: int = Field(default=3, description="Max retries before marking issue as failed")
    max_concurrent_dispatches: int = Field(
        default=50, description="Max concurrent (sentry_org, git_org) dispatches per tick"
    )


class SentryWatcherConfig(BaseModel):
    """Configuration for the sentry container watcher."""

    enabled: bool = Field(default=True, description="Enable container watching")
    reconcile_interval: int = Field(default=60, description="Seconds between reconciliation scans")


class SentryPluginConfig(BaseModel):
    """Configuration for the Sentry API plugin."""

    enabled: bool = False
    base_url: str = "https://sentry.io"
    auth_token: str = ""
    client_secret: str = ""
    webhook_url: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    dispatch: SentryDispatchConfig = Field(default_factory=SentryDispatchConfig)
    watcher: SentryWatcherConfig = Field(default_factory=SentryWatcherConfig)
