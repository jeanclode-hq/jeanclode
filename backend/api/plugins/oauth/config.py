"""OAuth plugin configuration."""

from pydantic import BaseModel


class OAuthPluginConfig(BaseModel):
    """OAuth plugin configuration.

    No provider-specific config — reads OAuth credentials from
    the GitHub and GitLab provider plugins.
    """

    enabled: bool = False
