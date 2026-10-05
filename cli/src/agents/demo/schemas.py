"""Pydantic schemas for the demo agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DemoInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # "pr": a respond mention asked for it; issue_* then describe the PR/MR.
    source: Literal["issue", "pr"] = "issue"
    issue_url: str = ""
    issue_title: str = ""
    issue_body: str = ""
    comments: str = ""
    findings: str = ""
    demo_plan: str = ""
    diff: str = ""
    # The fixer's hints.md, when it wrote one after an earlier round.
    hints: str = ""
    # Every worktree the fix touches, as ``{"name", "path"}``.
    repos: list[dict[str, str]] = Field(default_factory=list)
    demo_dir: str = ""
    round: int = 1
    max_rounds: int = 6


class DemoOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["ok", "broken", "unavailable", "nothing_to_show"]
    evidence: str = ""
    # Name of the checkout the app was launched from (one of DemoInput.repos, or a related repo).
    app_repo: str = ""
    # What the PR/MR gets: the screenshots for a static change, the video for an interaction.
    media: Literal["screenshot", "video"] = "video"
    video_path: str = ""
    screenshots: list[str] = Field(default_factory=list)
