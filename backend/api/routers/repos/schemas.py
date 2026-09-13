"""Schemas for repos endpoints."""

import uuid

from pydantic import BaseModel, Field


class RelatedRepo(BaseModel):
    """A repo grouped with another one, as shown on its row."""

    id: uuid.UUID = Field(description="Repository ID")
    root_org_id: uuid.UUID = Field(description="Connected org at the root of the hierarchy")
    name: str = Field(description="Repository name")


class RepoResponse(BaseModel):
    """Repository response schema."""

    id: uuid.UUID = Field(description="Repository ID")
    org_id: uuid.UUID = Field(description="Parent git organization ID")
    root_org_id: uuid.UUID = Field(
        description="Connected org at the root of the hierarchy — equals org_id "
        "unless the repo sits in a GitLab subgroup"
    )
    name: str = Field(description="Repository name")
    external_id: str = Field(description="External repo ID from platform")
    provider: str = Field(description="Provider type (github or gitlab)")
    web_url: str | None = Field(description="Repository web URL")
    avatar_url: str | None = Field(description="Repository avatar URL")
    enabled: bool = Field(default=True, description="Whether triggers are active for this repo")
    related: list[RelatedRepo] = Field(
        default_factory=list,
        description="Repos grouped with this one — inlined so the repo list "
        "needs one request for a page rather than one per row",
    )


class RepoPage(BaseModel):
    """One page of an organization's repositories."""

    items: list[RepoResponse]
    total: int = Field(description="Total repositories matching the query")
    enabled_count: int = Field(
        default=0, description="How many matching repositories have triggers enabled"
    )
    page: int = Field(description="1-indexed page number")
    has_more: bool = Field(description="Whether another page exists")


class LinkRepoRequest(BaseModel):
    """Request body for linking two repos into a group."""

    related_repo_id: uuid.UUID = Field(description="Repository ID to group with this one")


class BulkRepoSettingsRequest(BaseModel):
    """Request body for enabling/disabling every repo of an organization."""

    org_id: uuid.UUID = Field(description="Organization whose repositories to update")
    enabled: bool = Field(description="Whether triggers should be active")
    search: str | None = Field(
        default=None,
        description="Restrict to repositories matching this name filter, as in GET /repos",
    )


class BulkRepoSettingsResponse(BaseModel):
    """Outcome of a bulk repo enable/disable."""

    updated: int = Field(description="Number of repositories updated")
