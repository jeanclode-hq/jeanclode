"""GitLab plugin configuration."""

from pydantic import BaseModel, Field


class GitLabOAuthConfig(BaseModel):
    """GitLab OAuth configuration."""

    client_id: str
    client_secret: str
    instance_url: str = "https://gitlab.com"
    scopes: list[str] = Field(default_factory=lambda: ["read_user", "read_api"])


class GitLabWatcherConfig(BaseModel):
    """Configuration for the gitlab container watcher."""

    enabled: bool = Field(default=True, description="Enable container watching")
    reconcile_interval: int = Field(default=60, description="Seconds between reconciliation scans")


class GitLabPluginConfig(BaseModel):
    """GitLab plugin configuration."""

    enabled: bool = False
    oauth: GitLabOAuthConfig | None = None
    webhook_secret: str | None = None
    webhook_url: str | None = None
    watcher: GitLabWatcherConfig = Field(default_factory=GitLabWatcherConfig)
