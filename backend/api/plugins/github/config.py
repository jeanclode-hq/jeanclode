"""GitHub plugin configuration."""

from pydantic import BaseModel, Field, model_validator


class GitHubAppConfig(BaseModel):
    """Unified GitHub App configuration.

    Supports both user-to-server OAuth (for user identity) and installation
    authentication (for repository access).

    The private key can be supplied either inline (``private_key_pem``) or via
    a filesystem path (``private_key_path``) — exactly one must be set.
    """

    # User-to-server OAuth credentials (for user login)
    client_id: str
    client_secret: str
    authorize_url: str = "https://github.com/login/oauth/authorize"
    token_url: str = "https://github.com/login/oauth/access_token"
    api_base_url: str = "https://api.github.com/"
    scopes: list[str] = Field(default_factory=lambda: ["read:user", "user:email", "read:org"])

    # Installation authentication credentials (for repository access)
    app_id: str
    private_key_path: str | None = None
    private_key_pem: str | None = None
    webhook_secret: str
    name: str

    @model_validator(mode="after")
    def _at_most_one_private_key_source(self) -> GitHubAppConfig:
        # Both empty is allowed: the app isn't configured via env vars and
        # will be loaded from DB-stored settings at runtime (or remain
        # disabled). Both set at once is ambiguous and always an error.
        if self.private_key_path and self.private_key_pem:
            raise ValueError(
                "GitHubAppConfig accepts at most one of 'private_key_path' or 'private_key_pem'"
            )
        return self


class GitHubWatcherConfig(BaseModel):
    """Configuration for the github container watcher."""

    enabled: bool = Field(default=True, description="Enable container watching")
    reconcile_interval: int = Field(default=60, description="Seconds between reconciliation scans")


class GitHubPluginConfig(BaseModel):
    """GitHub plugin configuration."""

    enabled: bool = False
    app: GitHubAppConfig | None = None
    watcher: GitHubWatcherConfig = Field(default_factory=GitHubWatcherConfig)
