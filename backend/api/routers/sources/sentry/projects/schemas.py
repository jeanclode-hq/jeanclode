"""Schemas for source project endpoints."""

import uuid

from pydantic import BaseModel, Field


class SentryProjectResponse(BaseModel):
    """Response schema for a source project."""

    id: uuid.UUID = Field(description="Repository database ID")
    external_id: str = Field(description="External project ID")
    name: str = Field(description="Project name")
    mapped_repo_id: uuid.UUID | None = Field(description="Mapped repository ID")
    mapping_method: str | None = Field(description="How the mapping was created")


class UpdateProjectRequest(BaseModel):
    """Request body for manual mapping override."""

    repo_id: uuid.UUID | None = Field(description="Repository ID to map to, or null to clear")


class ResolveMappingsResponse(BaseModel):
    """Response for queued mapping resolution."""

    status: str = Field(description="Accepted status")


class ResolveMappingsMessage(BaseModel):
    """Message schema for mapping resolution jobs.

    One message per Sentry org — the consumer resolves against the union of
    all git orgs linked to the same workspace.
    """

    sentry_org_id: str = Field(description="Sentry Organization database ID (UUID)")
