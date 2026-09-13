"""Schemas for the config endpoint."""

from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """Which providers are enabled."""

    github_enabled: bool = Field(description="Whether GitHub integration is available")
    gitlab_enabled: bool = Field(description="Whether GitLab integration is available")
    llm_enabled: bool = Field(default=False, description="Whether an LLM provider is configured")
    github_app_install_url: str | None = Field(
        default=None, description="GitHub App installation URL"
    )
    gitlab_instance_url: str | None = Field(
        default=None, description="Admin-configured GitLab instance URL"
    )


class AppConfig(BaseModel):
    """Application configuration exposed to the frontend."""

    providers: ProviderConfig
    setup_required: bool = Field(
        default=False,
        description="True when no git provider or LLM is configured; admin page is gated until both are set.",
    )
    admin_enabled: bool = Field(
        default=True,
        description="False when the instance is fully configured via env vars; admin UI is hidden since env precedence would shadow any edits.",
    )
