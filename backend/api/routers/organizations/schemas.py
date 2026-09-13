"""Schemas for organization endpoints."""

import uuid

from pydantic import BaseModel, Field


class OrgResponse(BaseModel):
    """Response schema for an organization."""

    id: uuid.UUID = Field(description="Organization database ID")
    name: str = Field(description="Organization name")
    provider: str = Field(description="Provider (github/gitlab/sentry)")
    external_org_id: str = Field(description="External org ID from provider")
    base_url: str | None = Field(default=None, description="Provider instance URL")
    avatar_url: str | None = Field(default=None, description="Organization avatar URL")
    onboarding_step: str = Field(description="Current onboarding step")
    repo_count: int = Field(default=0, description="Number of repositories")
    created_at: str = Field(description="Creation timestamp")


class UpdateOrgRequest(BaseModel):
    """Request body for updating an organization."""

    onboarding_step: str | None = Field(default=None, description="Onboarding step to set")


class ClaimOrgRequest(BaseModel):
    """Request body for claiming an orphaned organization into a workspace."""

    installation_id: str = Field(description="App installation ID")
    workspace_id: uuid.UUID = Field(description="Workspace to assign the org to")


class OrgStatsResponse(BaseModel):
    """Per-organization stats for the dashboard."""

    # Sentry orgs (issue source)
    total_issues: int = Field(default=0, description="Total issues tracked")
    issues_fixing: int = Field(default=0, description="Issues currently being fixed")
    issues_fixed: int = Field(default=0, description="Issues with completed fixes")
    issues_failed: int = Field(default=0, description="Issues with failed fixes")
    fix_success_rate: float = Field(default=0.0, description="Fix success rate")
    # Git orgs (PR/repo source)
    repo_count: int = Field(default=0, description="Number of repositories")
    total_prs: int = Field(default=0, description="Total pull requests")
    prs_reviewed: int = Field(default=0, description="PRs reviewed by bot")
    prs_open: int = Field(default=0, description="PRs currently open")
    # Latest activity
    latest_title: str | None = Field(default=None, description="Latest activity title")
    latest_pr_url: str | None = Field(default=None, description="Latest activity PR URL")
    latest_pr_number: int | None = Field(default=None, description="Latest activity PR number")
    latest_at: str | None = Field(default=None, description="Latest activity timestamp")


class SyncOrgRepositoriesResponse(BaseModel):
    """Result of a manual repository re-sync request."""

    queued: bool = Field(description="Whether the sync job was queued")
    provider: str = Field(description="Provider the sync runs against")
    message: str = Field(description="Human-readable outcome")


class OrgMemberResponse(BaseModel):
    """One member of a connected org, as offered in the notify picker."""

    provider_identity_id: uuid.UUID = Field(description="Provider identity ID — the stable handle")
    username: str = Field(description="Provider username (the @-mention handle)")
    avatar_url: str | None = Field(default=None, description="Provider avatar URL")
    provider: str = Field(description="Provider (github/gitlab)")
    role: str = Field(description="Membership role")
    has_account: bool = Field(description="Whether this member has ever logged into Jeanclode")


class OrgMembersResponse(BaseModel):
    """All members of a connected org and its ancestors."""

    members: list[OrgMemberResponse] = Field(default_factory=list)
    total: int = Field(default=0, description="Number of members returned")


class OrgSubgroupResponse(BaseModel):
    """One subgroup org, for the subgroup-pack exclusion picker."""

    id: uuid.UUID = Field(description="Subgroup organization ID")
    name: str = Field(description="Subgroup name")
    repo_count: int = Field(description="Repositories hanging directly off this subgroup")


class OrgSubgroupsResponse(BaseModel):
    """Every subgroup below a connected organization."""

    items: list[OrgSubgroupResponse] = Field(default_factory=list)
    max_pack_size: int = Field(
        description="Subgroups holding more repos than this are never packed"
    )
