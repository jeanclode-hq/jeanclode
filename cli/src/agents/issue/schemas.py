"""Pydantic schemas for the issue-resolve agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TriageInput(BaseModel):
    """Render-time input for the issue triage prompt (Jinja2 variables)."""

    model_config = ConfigDict(extra="ignore")

    issue_url: str = ""
    provider: str = "github"
    repo: str = ""
    # Local directory name of the primary repo's clone, e.g. "jdoe_qa-webshop"
    # — same naming convention as the "Related repositories" block, so
    # triage can name the primary in target_repos symmetrically with any
    # related repo instead of it being an implicit default.
    repo_name: str = ""
    issue_number: str = ""
    issue_title: str = ""
    issue_body: str = ""
    comments: str = ""


class TriageOutput(BaseModel):
    """7-outcome triage result from the issue triage agent."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal[
        "proceed",
        "needs_info",
        "push_back",
        "duplicate",
        "already_fixed",
        "refuse",
        "split",
    ]
    reasoning: str = ""
    comment_body: str = ""
    # Populated only for "proceed". The complete list of repo names that
    # need a code change — the primary (use TriageInput.repo_name) and/or
    # any related repos (matching entries in ctx.related_repos), by name,
    # in any combination. Nothing is included automatically: a repo only
    # gets a worktree if it's listed here. The common case is just
    # [repo_name]. A fix that lives entirely in a related repo omits the
    # primary's name — leaving it out of an unrelated repo's empty
    # scaffold means that repo is never touched, worktree included.
    target_repos: list[str] = Field(default_factory=list)
    # Populated only for "proceed". Handoff notes for the fixer agent:
    # root cause, relevant files/functions, suggested approach. Triage
    # already did this investigation to reach its "proceed" decision — this
    # captures it instead of throwing it away and re-deriving it later.
    findings: str = ""


class IssueFixerInput(BaseModel):
    """Render-time input for the issue fixer prompt."""

    model_config = ConfigDict(extra="ignore")

    issue_url: str = ""
    branch: str = ""
    findings: str = ""
    # Every repo the fixer has a worktree for — always at least one entry,
    # but not necessarily the primary (a fix that lives entirely in a
    # related repo has only that repo here). When there's exactly one,
    # cwd IS that repo. When there's more than one, cwd is their shared
    # parent and each entry's "path" is where that repo's worktree
    # actually lives.
    repos: list[dict[str, str]] = Field(default_factory=list)


class IssueFixerOutput(BaseModel):
    """JSON the issue fixer agent emits — one session may span several repos."""

    model_config = ConfigDict(extra="ignore")

    changes_summary: str = ""
    static_check_passed: bool = False
