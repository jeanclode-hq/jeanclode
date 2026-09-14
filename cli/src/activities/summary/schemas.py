from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PRSnapshot(BaseModel):
    """Plain-text PR/MR context populated by the adaptor preflight.

    Mirrors the files written under ``<cwd>/.context/`` by the GitHub
    and GitLab adaptors. Loading them once into a typed object keeps the
    workflow readable and lets activities take a single argument.
    """

    model_config = ConfigDict(extra="ignore")

    platform: Literal["github", "gitlab"]
    repo: str
    pr: str
    pr_url: str = ""
    pr_description: str = ""
    diff: str = ""


class FileLine(BaseModel):
    """One row of the per-file dropdown."""

    model_config = ConfigDict(extra="ignore")

    path: str
    old_path: str = ""
    additions: int = 0
    deletions: int = 0
    summary: str = ""


class ParsedSummary(BaseModel):
    """The refined summary, ready to be rendered.

    Just the prose: links live inside it, written by the summarizer with
    the relationship they carry ("fixes", "depends on"), rather than
    being stripped out and re-listed as a bare section.
    """

    model_config = ConfigDict(extra="ignore")

    description: str
    files: list[FileLine] = []


class SummaryPayload(BaseModel):
    """Final markdown body posted to the PR/MR."""

    model_config = ConfigDict(extra="ignore")

    body: str


class PostResult(BaseModel):
    """Outcome of updating the PR/MR description."""

    model_config = ConfigDict(extra="ignore")

    posted: bool = False
    pr_url: str = ""
    error: str = ""
