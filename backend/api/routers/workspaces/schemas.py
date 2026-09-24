"""Request and response schemas for workspace endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer


class WorkspaceResponse(BaseModel):
    """Workspace response."""

    id: UUID = Field(description="Workspace ID")
    name: str = Field(description="Workspace name")
    slug: str = Field(description="Workspace slug")
    created_at: datetime = Field(description="Creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Convert UUID to string."""
        return str(value)


class WorkspaceListResponse(BaseModel):
    """List of workspaces response."""

    workspaces: list[WorkspaceResponse]


class CreateWorkspaceRequest(BaseModel):
    """Request to create a workspace."""

    name: str = Field(min_length=1, max_length=255, description="Workspace name")


class SourceSummary(BaseModel):
    """Compact org info for workspace source listing."""

    id: UUID = Field(description="Org ID")
    name: str = Field(description="Org name")
    provider: str = Field(description="Provider type (sentry, github, gitlab)")
    avatar_url: str | None = Field(default=None, description="Avatar URL")

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Convert UUID to string."""
        return str(value)


class WorkspaceSourcesResponse(BaseModel):
    """Connected sources for a workspace."""

    sources: list[SourceSummary]


class WorkspaceExecutionResponse(BaseModel):
    """A single execution for the dashboard grid."""

    id: UUID = Field(description="Execution ID")
    issue_title: str = Field(description="Title of the related issue")
    source: str = Field(description="Source provider (sentry, github, etc.)")
    source_name: str | None = Field(default=None, description="Organization name")
    source_avatar_url: str | None = Field(default=None, description="Organization avatar URL")
    repo_name: str | None = Field(default=None, description="Repository name")
    workflow: str = Field(description="Workflow type (fix, review)")
    kind: str = Field(description="Target type: issue, pull_request, or unknown")
    status: str = Field(description="Execution status")
    current_step: str | None = Field(default=None, description="Current pipeline step")
    error_type: str | None = Field(default=None, description="Error type if failed")
    error_detail: str | None = Field(default=None, description="Error detail if failed")
    pr_url: str | None = Field(default=None, description="Pull request URL")
    pr_number: int | None = Field(default=None, description="Pull request number")
    prompt_text: str | None = Field(
        default=None, description="Mention/comment text that triggered a RESPOND execution"
    )
    started_at: datetime = Field(description="When the execution started")
    completed_at: datetime | None = Field(default=None, description="When completed")
    duration_seconds: float | None = Field(default=None, description="Duration in seconds")

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Convert UUID to string."""
        return str(value)


class WorkspaceExecutionsResponse(BaseModel):
    """Paginated list of executions for the dashboard."""

    items: list[WorkspaceExecutionResponse]
    total: int
    page: int
    has_more: bool


class ActiveExecutionsResponse(BaseModel):
    """Running then queued executions for the dashboard."""

    items: list[WorkspaceExecutionResponse]


class LgtmEntry(BaseModel):
    """LGTM leaderboard entry."""

    username: str = Field(description="PR author username")
    avatar_url: str | None = Field(default=None, description="Author avatar URL")
    lgtm_count: int = Field(description="Number of bot-approved reviews")


class PrAuthorEntry(BaseModel):
    """Top PR author leaderboard entry."""

    username: str = Field(description="PR author username")
    avatar_url: str | None = Field(default=None, description="Author avatar URL")
    pr_count: int = Field(description="Number of pull requests created")


class ActiveRepoEntry(BaseModel):
    """Most active repo leaderboard entry."""

    name: str = Field(description="Repository name")
    provider: str = Field(description="Provider (github/gitlab)")
    execution_count: int = Field(description="Total completed executions")


class LeaderboardsResponse(BaseModel):
    """All leaderboards for a workspace."""

    lgtm: list[LgtmEntry]
    top_pr_authors: list[PrAuthorEntry]
    active_repos: list[ActiveRepoEntry]


class WorkspaceMemberResponse(BaseModel):
    """A single workspace member entry."""

    user_id: UUID = Field(description="User ID")
    username: str = Field(description="Display username")
    avatar_url: str | None = Field(default=None, description="Avatar URL")
    providers: list[str] = Field(
        default_factory=list, description="Connected providers (github/gitlab)"
    )
    joined_at: datetime = Field(description="When the user joined the workspace")

    @field_serializer("user_id")
    def serialize_user_id(self, value: UUID) -> str:
        """Convert UUID to string."""
        return str(value)


class WorkspaceMembersResponse(BaseModel):
    """Paginated workspace members list."""

    members: list[WorkspaceMemberResponse]
    total: int
    page: int
    has_more: bool


class TopUserEntry(BaseModel):
    """Someone who @-mentioned the bot, with how many times."""

    identity_id: UUID
    username: str | None
    avatar_url: str | None
    provider: str
    pings: int


class DashboardStatsSection(BaseModel):
    """Dashboard cards. Runs, reviews and pings cover the stats window."""

    running: int = Field(description="Executions running now, any workflow")
    queued: int = Field(description="Executions waiting to start")
    successful_runs: int = Field(description="Completed executions in the window")
    reviewed_prs: int = Field(description="PRs with a completed review in the window")
    top_users: list[TopUserEntry] = Field(description="Who pinged the bot most in the window")


class IssueStatsSection(BaseModel):
    """Issues page cards, all-time."""

    handled: int = Field(description="Issues triaged not actionable, or fixed with a PR")
    prs_created: int = Field(description="PRs Jeanclode opened for issues")
    prs_merged: int = Field(description="Of those, merged")


class PullRequestStatsSection(BaseModel):
    """PR page cards, all-time."""

    reviewed: int = Field(description="PRs with a completed review")
    pending_review: int = Field(description="Open PRs on enabled repos never reviewed")
    summarized: int = Field(description="PRs with a completed summary")


class WorkspaceStatsResponse(BaseModel):
    """Stat cards for the dashboard, issues and PR pages."""

    window_days: int
    dashboard: DashboardStatsSection
    issues: IssueStatsSection
    pull_requests: PullRequestStatsSection
