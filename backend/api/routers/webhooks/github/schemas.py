"""GitHub webhook payload schemas."""

from typing import Any

from pydantic import BaseModel, Field


class GitHubAppInstallationEvent(BaseModel):
    """GitHub App installation webhook payload."""

    action: str = Field(description="Event action (created, deleted, etc.)")
    installation: dict[str, Any] = Field(description="Installation object")
    repositories: list[dict[str, Any]] | None = Field(
        description="Repositories affected", default=None
    )
    sender: dict[str, Any] = Field(description="User who triggered the event")


class GitHubAppInstallationRepositoriesEvent(BaseModel):
    """GitHub App installation_repositories webhook payload."""

    action: str = Field(description="Event action (added, removed)")
    installation: dict[str, Any] = Field(description="Installation object")
    repositories_added: list[dict[str, Any]] | None = Field(
        description="Repositories that were added", default=None
    )
    repositories_removed: list[dict[str, Any]] | None = Field(
        description="Repositories that were removed", default=None
    )
    sender: dict[str, Any] = Field(description="User who triggered the event")


class GitHubRepositoryEvent(BaseModel):
    """GitHub ``repository`` webhook payload (created, deleted, renamed, ...).

    Complements ``installation_repositories``: an org-wide installation does
    not always surface a brand new repo through the installation event, and a
    dropped delivery leaves the repo invisible either way.
    """

    action: str = Field(description="Event action (created, deleted, renamed, transferred)")
    repository: dict[str, Any] = Field(description="Repository object")
    installation: dict[str, Any] | None = Field(
        default=None, description="Installation the event was delivered for"
    )
    changes: dict[str, Any] | None = Field(
        default=None, description="Previous values, present on renamed/transferred"
    )


class GitHubOrganizationMembershipEvent(BaseModel):
    """GitHub organization webhook payload for member_added / member_removed."""

    action: str = Field(description="Event action (member_added, member_removed)")
    membership: dict[str, Any] = Field(description="Membership object with user and role")
    organization: dict[str, Any] = Field(description="Organization object")
    sender: dict[str, Any] = Field(description="User who triggered the event")


class GitHubSyncInstallationMessage(BaseModel):
    """Message schema for GitHub installation sync jobs."""

    installation_id: str = Field(description="GitHub App installation ID")
    sender_github_id: str | None = Field(
        default=None, description="GitHub user ID who installed the app"
    )


class GitHubSyncRepositoriesMessage(BaseModel):
    """Message schema for backfilling a specific set of installation repos.

    Emitted whenever repos appear outside the initial installation sync
    (``installation_repositories.added``, ``repository.created``, a manual
    re-sync): the webhook payload alone carries no PRs or issues, and its
    repo entries are too thin to build a complete row from.
    """

    installation_id: str = Field(description="GitHub App installation ID")
    external_repo_ids: list[str] = Field(description="GitHub repository IDs to sync")
