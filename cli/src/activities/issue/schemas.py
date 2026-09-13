"""Pydantic schemas shared across the issue-resolve activities + workflow."""

from __future__ import annotations

from pydantic import BaseModel


class IssueContext(BaseModel):
    """Pre-fetched issue data passed to the triage agent."""

    issue_url: str
    provider: str  # "github" | "gitlab"
    repo: str  # "owner/repo" (github) or "host/project/path" (gitlab)
    issue_number: str
    issue_title: str
    issue_body: str
    comments: str
