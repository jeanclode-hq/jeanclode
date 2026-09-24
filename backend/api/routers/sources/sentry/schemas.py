"""Schemas for Sentry source endpoints."""

import uuid

from pydantic import BaseModel, Field

from api.models.settings import BackfillScope


class LinkSentrySourceRequest(BaseModel):
    """Request schema for linking a Sentry org to a workspace."""

    workspace_id: uuid.UUID = Field(description="Workspace ID to link Sentry to")
    org_slug: str = Field(description="Sentry organization slug (from your Sentry URL)")
    auth_token: str = Field(description="Sentry auth token from the integration")
    client_secret: str = Field(
        description="Sentry integration client secret for webhook verification"
    )
    base_url: str = Field(default="https://sentry.io", description="Sentry instance URL")
    backfill_scope: BackfillScope | None = Field(
        default=None,
        description=(
            "How much existing Sentry history to import on connect. Omit to "
            "keep an already-linked org's stored scope; new orgs default to "
            "importing nothing."
        ),
    )


class SentrySourceResponse(BaseModel):
    """Response schema for Sentry source linking."""

    org_id: str = Field(description="Organization ID")
    org_slug: str = Field(description="Sentry organization slug")
    projects_synced: bool = Field(description="Whether project sync was queued")


class SentrySyncProjectsMessage(BaseModel):
    """Message schema for Sentry project sync jobs."""

    org_id: str = Field(description="Organization database ID (UUID)")
    org_slug: str = Field(description="Sentry organization slug")


class SentrySyncMembershipsMessage(BaseModel):
    """Message schema for Sentry membership sync jobs."""

    org_id: str = Field(description="Organization database ID (UUID)")
    org_slug: str = Field(description="Sentry organization slug")
