"""Schemas for admin + setup endpoints."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AdminAuthRequest(BaseModel):
    """Body for POST /admin/auth."""

    secret: str


class AdminAuthResponse(BaseModel):
    """Response for POST /admin/auth."""

    message: str = "Authenticated"


class GitHubConfigInput(BaseModel):
    """Input for saving GitHub App credentials."""

    client_id: str
    client_secret: str
    app_id: str
    private_key_pem: str
    webhook_secret: str
    name: str
    owner_login: str | None = None
    owner_type: str | None = None  # "User" or "Organization"


class GitHubConfigView(BaseModel):
    """GitHub config with secrets masked."""

    client_id: str | None = None
    client_secret: str | None = None  # "****" when present
    app_id: str | None = None
    private_key_pem: str | None = None  # "****" when present
    webhook_secret: str | None = None  # "****" when present
    name: str | None = None
    owner_login: str | None = None
    owner_type: str | None = None


class GitLabConfigInput(BaseModel):
    """Input for saving GitLab OAuth credentials + webhook secret."""

    client_id: str
    client_secret: str
    instance_url: str = "https://gitlab.com"
    webhook_secret: str = ""


class GitLabConfigView(BaseModel):
    """GitLab config with secrets masked."""

    client_id: str | None = None
    client_secret: str | None = None
    instance_url: str | None = None
    webhook_secret: str | None = None  # "****" when present


class AdminSettingsResponse(BaseModel):
    """Response for GET /admin/settings."""

    github: GitHubConfigView | None = None
    gitlab: GitLabConfigView | None = None


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str


class AdminCategory(BaseModel):
    """Path param validator for delete."""

    category: Literal["github", "gitlab"] = Field(...)


class LLMCredentialInput(BaseModel):
    """Input for creating/updating one credential in the LLM pool (ADR-010).

    ``model_high`` is used by workflows that need strong reasoning (e.g. the
    Sentry fix-drafting agent). ``model_low`` is for cheap utility calls
    (summaries, parsing). Workflows pick whichever tier fits the task.

    ``kind`` is informational metadata distinguishing a pay-per-token API
    key from an OAuth subscription token — both use the ``secret`` field.
    ``claude_code`` uses an OAuth token from ``claude setup-token`` and runs
    against the user's Anthropic subscription.
    """

    kind: Literal["api_key", "oauth_subscription"]
    provider: Literal["claude_code", "anthropic", "openai", "openai_compatible"]
    secret: str  # OAuth token when provider is "claude_code"
    model_high: str = ""
    model_low: str = ""
    base_url: str | None = None
    plan_tier: Literal["pro", "max", "max_5x"] | None = None

    @model_validator(mode="after")
    def _validate_provider_fields(self) -> LLMCredentialInput:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("base_url is required when provider is 'openai_compatible'")
        if not self.model_high:
            raise ValueError(f"model_high is required when provider is '{self.provider}'")
        if not self.model_low:
            raise ValueError(f"model_low is required when provider is '{self.provider}'")
        return self


class LLMCredentialUpdateInput(BaseModel):
    """Input for updating a credential — every field optional so a rotate
    can touch only ``secret`` without retyping the rest."""

    kind: Literal["api_key", "oauth_subscription"] | None = None
    provider: Literal["claude_code", "anthropic", "openai", "openai_compatible"] | None = None
    secret: str | None = None
    model_high: str | None = None
    model_low: str | None = None
    base_url: str | None = None
    plan_tier: Literal["pro", "max", "max_5x"] | None = None


class LLMCredentialView(BaseModel):
    """One credential in the pool, with its secret masked."""

    id: str
    priority: int
    kind: str
    provider: str
    plan_tier: str | None = None
    secret: str | None = None  # "****" when present
    model_high: str
    model_low: str
    base_url: str | None = None
    status: str
    stale_until: str | None = None


class LLMCredentialReorderInput(BaseModel):
    """Body for POST /admin/llm-credentials/reorder — the full, reordered
    list of credential ids (index 0 gets priority 1)."""

    ordered_ids: list[str]


class GithubManifestResponse(BaseModel):
    """Manifest payload + CSRF state for the GitHub App manifest flow."""

    manifest: dict
    state: str
    github_url: str = Field(
        default="https://github.com/settings/apps/new",
        description=(
            "Where to POST the manifest form. Personal: "
            "``https://github.com/settings/apps/new``. Org: "
            "``https://github.com/organizations/{org}/settings/apps/new``."
        ),
    )
