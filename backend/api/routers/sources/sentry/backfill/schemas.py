"""Schemas for Sentry issue backfill."""

from pydantic import BaseModel, Field

from api.models.settings import BackfillScope


class BackfillIssuesMessage(BaseModel):
    """Message schema for issue backfill jobs."""

    org_id: str = Field(description="Organization database ID (UUID)")
    org_slug: str = Field(description="Sentry organization slug")
    scope: BackfillScope | None = Field(
        default=None,
        description=(
            "Explicit scope for this run. Manual triggers set it so the run "
            "proceeds regardless of the org's stored preference; automatic "
            "chains leave it unset and the org setting decides."
        ),
    )


class BackfillRequest(BaseModel):
    """Request body for a manual backfill trigger."""

    scope: BackfillScope | None = Field(
        default=None,
        description=(
            "How much history to import for this run. Defaults to the org's "
            "configured scope, or 30 days when that scope is 'none'."
        ),
    )


class BackfillResponse(BaseModel):
    """Response for queued backfill."""

    status: str = Field(description="Accepted status")
    scope: BackfillScope = Field(description="Scope the queued run will use")
