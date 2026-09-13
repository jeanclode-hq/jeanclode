"""Application options schema.

Jeanclode-specific optional configuration accessible via app.options.
"""

from pydantic import BaseModel, Field


class ClaudeCodeOptions(BaseModel):
    """Options for Claude Code authentication in CLI containers."""

    oauth_token: str | None = Field(
        default=None,
        description="Claude Code OAuth token (from `claude setup-token`, for subscription users)",
    )
    api_key: str | None = Field(
        default=None,
        description="Anthropic API key for Claude models",
    )
    model_high: str | None = Field(
        default=None,
        description=(
            "JEANCLODE_MODEL for containers dispatched via this env-var "
            "override path (high-reasoning workflows). Unset falls back to "
            "the CLI's own default (sonnet) — the DB credential pool's "
            "model_high has no bearing here since this path bypasses the pool."
        ),
    )
    model_low: str | None = Field(
        default=None,
        description="JEANCLODE_SMALL_MODEL counterpart of model_high, for cheap utility calls.",
    )


class Options(BaseModel):
    """Root options container.

    Access via app.options.claude_code.api_key, etc.
    """

    claude_code: ClaudeCodeOptions = Field(default_factory=ClaudeCodeOptions)
    backend_url: str | None = Field(
        default=None,
        description=(
            "This backend's own publicly reachable base URL. Used for "
            "container-initiated self-calls, e.g. the /internal/memory API "
            "(set via BACKEND_URL)."
        ),
    )
