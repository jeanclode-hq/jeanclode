"""Schemas for issues endpoints."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PullRequestSummary(BaseModel):
    """Summary of a pull/merge request opened by an execution."""

    id: uuid.UUID = Field(description="Pull request ID")
    pr_number: int = Field(description="PR/MR number")
    title: str = Field(description="PR/MR title")
    state: str = Field(description="PR/MR state (open, closed, merged)")
    pr_url: str = Field(description="URL to the PR/MR")


class ExecutionSummary(BaseModel):
    """Summary of an execution for issue detail view."""

    id: uuid.UUID = Field(description="Execution ID")
    workflow: str = Field(description="Workflow type (fix, review)")
    trigger: str = Field(description="How execution was triggered (auto, manual)")
    status: str = Field(description="Execution status")
    container_id: str | None = Field(description="Container ID")
    error_type: str | None = Field(description="Error type if failed")
    error_detail: str | None = Field(description="Error detail if failed")
    created_at: datetime = Field(description="Execution created timestamp")
    fixer_llm_credential: str | None = Field(
        default=None, description="LLM credential the issue-resolve fixer ran on"
    )
    fixer_llm_model: str | None = Field(
        default=None, description="Model the issue-resolve fixer ran on"
    )
    fixer_llm_reason: str | None = Field(
        default=None, description="Why triage picked that credential or model"
    )
    pull_requests: list[PullRequestSummary] = Field(
        default_factory=list, description="Pull/merge requests opened by this execution"
    )


class IssueResponse(BaseModel):
    """Issue list item response."""

    id: uuid.UUID = Field(description="Issue ID")
    external_id: str = Field(description="External issue ID (e.g. Sentry issue ID)")
    title: str = Field(description="Issue title")
    culprit: str | None = Field(None, description="Issue culprit")
    level: str = Field(description="Issue severity level")
    status: str = Field(description="Source status (open/closed, unresolved/resolved)")
    execution_status: str = Field(
        "pending",
        description=(
            "Raw execution status: pending (no execution yet), queued, running, "
            "completed, failed, cancelled"
        ),
    )
    execution_id: uuid.UUID | None = Field(None, description="ID of the latest execution")
    result: str = Field(
        "pending",
        description=(
            "Computed outcome combining execution + PR + triage state: pending, "
            "running, failed, completed, pr_open, pr_merged, rejected, not_actionable"
        ),
    )
    event_count: int = Field(description="Number of events")
    first_seen: datetime | None = Field(None, description="First seen timestamp")
    last_seen: datetime | None = Field(None, description="Last seen timestamp")
    author: str | None = Field(None, description="Issue author/reporter")
    source: str = Field(description="Issue source (sentry, linear, etc.)")
    source_org_id: uuid.UUID = Field(description="Source organization ID")
    project: str = Field(description="Project name/slug")
    issue_url: str | None = Field(None, description="External URL to the issue")
    triage_result: str | None = Field(None, description="Triage outcome")
    workflow: str | None = Field(None, description="Workflow type of latest execution")
    has_mapping: bool = Field(False, description="Whether the issue's repo has a git mapping")
    repo_enabled: bool = Field(True, description="Whether the repo has triggers enabled")
    created_at: datetime = Field(description="Created timestamp")


class IssueDetailResponse(IssueResponse):
    """Issue detail response with extra fields."""

    triage_metadata: dict[str, Any] | None = Field(None, description="Full triage result JSON")
    executions: list[ExecutionSummary] = Field(
        default_factory=list, description="Execution history"
    )


class RepoOption(BaseModel):
    """Repository option for a repo filter dropdown (issues or pull requests)."""

    id: uuid.UUID = Field(description="Repository ID")
    name: str = Field(description="Repository name")
    org_name: str = Field(description="Name of the org this repository belongs to")


class RepoOptionPage(BaseModel):
    """One page of repo filter options."""

    items: list[RepoOption]
    total: int = Field(description="Total repositories matching the query")
    page: int = Field(description="1-indexed page number")
    has_more: bool = Field(description="Whether another page exists")


class FixTriggerResponse(BaseModel):
    """Response from the manual fix trigger endpoint."""

    execution_id: uuid.UUID = Field(description="ID of the created execution")
    status: str = Field(description="Initial execution status")
