"""Schemas for GitLab sources endpoints."""

from pydantic import BaseModel, Field


class AddGitLabSourceRequest(BaseModel):
    """Request schema for adding a GitLab source."""

    access_token: str = Field(description="GitLab access token (group or project level)")
    gitlab_url: str = Field(
        default="https://gitlab.com",
        description="GitLab instance URL (default: https://gitlab.com)",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Workspace ID to assign the organization to (optional, auto-creates if omitted)",
    )
    manage_project_webhooks: bool = Field(
        default=False,
        description=(
            "Create the Jeanclode webhook on every project (for GitLab Free, which "
            "has no group webhooks). Also needs an instance system hook — without "
            "merge request events — for new projects to be discovered."
        ),
    )


class GitLabSourceResponse(BaseModel):
    """Response schema for GitLab source creation."""

    source_type: str = Field(description="Type of source: 'group'")
    source_id: str = Field(description="ID of created Organization")
    source_name: str = Field(description="Name of the group")
    repos_synced: bool = Field(description="Whether repo sync was queued")


class GitLabSyncGroupRepositoriesMessage(BaseModel):
    """Message schema for GitLab group repository sync jobs."""

    org_id: str = Field(description="Organization database ID (UUID)")
    group_id: str = Field(description="GitLab group ID to fetch projects from")


class GitLabSyncProjectRepositoryMessage(BaseModel):
    """Message schema for GitLab single-project repository sync jobs."""

    org_id: str = Field(description="Organization database ID (UUID)")
    project_id: str = Field(description="GitLab project ID to sync MRs and issues from")


class GitLabProjectHookTeardown(BaseModel):
    """One project and the encrypted token that covers it."""

    project_id: str = Field(description="GitLab project ID")
    provider_url: str = Field(description="GitLab instance URL for this project")
    encrypted_token: str = Field(description="Encrypted token covering this project")


class GitLabProjectHookTeardownMessage(BaseModel):
    """Delete the Jeanclode webhook from a set of projects.

    Each project carries its own token because a project-token setup keeps the
    token on the repo row, not the org — and the org rows may already be gone
    (org deletion) by the time the consumer runs.
    """

    projects: list[GitLabProjectHookTeardown] = Field(
        description="Projects to remove the hook from"
    )
