"""Schemas for pull requests endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PullRequestResponse(BaseModel):
    """Pull request list item."""

    id: uuid.UUID = Field(description="Pull request ID")
    title: str = Field(description="PR title")
    author: str = Field(description="PR author")
    pr_url: str = Field(description="Pull request URL")
    pr_number: int = Field(description="Pull request number")
    branch_name: str = Field(description="Head branch name")
    repo_name: str = Field(description="Repository name")
    provider: str = Field(description="Git provider: github, gitlab")
    status: str = Field(description="PR state: open, merged, closed")
    execution_status: str = Field(description="Latest execution status (none if no execution)")
    execution_id: uuid.UUID | None = Field(None, description="ID of the latest REVIEW execution")
    workflow: str | None = Field(
        None, description="Latest execution workflow: review, summary, fix"
    )
    error_type: str | None = Field(None, description="Error type if latest execution failed")
    error_detail: str | None = Field(None, description="Error detail if latest execution failed")
    execution_trigger: str | None = Field(
        None, description="How the latest execution was triggered"
    )
    execution_started_at: datetime | None = Field(
        None, description="When the latest execution started"
    )
    repo_enabled: bool = Field(True, description="Whether the repo has triggers enabled")
    created_at: datetime = Field(description="PR created timestamp")
    merged_at: datetime | None = Field(None, description="Merged timestamp")


class ReviewTriggerResponse(BaseModel):
    """Response for the manual review/summary trigger."""

    execution_id: uuid.UUID = Field(description="ID of the queued execution")
    status: str = Field(description="Initial execution status (queued)")
