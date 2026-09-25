"""Pydantic schemas shared across the sentry-fix activities + workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from src.activities.git.schemas import PRRef, WorktreePath

__all__ = [
    "GroupResult",
    "PRRef",
    "RoutingPlan",
    "SentryEvent",
    "SynthesisAgentOutput",
    "SynthesisGroup",
    "TriageOutput",
    "TriagedIssue",
    "WorktreePath",
]


class SentryEvent(BaseModel):
    """One Sentry issue, fetched and formatted as input to the triage agent."""

    issue_id: str
    sentry_url: str
    formatted: str
    repo_url: str | None = None
    commit_sha: str | None = None


class TriageOutput(BaseModel):
    """JSON shape the triage agent emits.

    Triage is also the planning stage (there is no separate planner): it
    reads the source to reach its verdict, so ``findings`` captures that
    investigation for the fixer instead of throwing it away and paying a
    second agent to re-derive it, and ``target_repos`` records which
    checkout(s) it decided the fix belongs in.
    """

    model_config = ConfigDict(extra="ignore")

    # `previously_rejected` is the closed-unmerged case: a human already
    # saw a fix for this root cause and declined it. Distinct from
    # `already_fixed` (landed) and from `duplicate` (an open PR/MR is
    # pending) — re-proposing rejected work is worse than not fixing.
    kind: Literal[
        "proceed",
        "not_actionable",
        "duplicate",
        "already_fixed",
        "previously_rejected",
    ] = "not_actionable"
    reason: str = ""
    confidence: float = 0.0
    affected_files: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str = ""
    category: str = ""
    # Populated only for "proceed". Handoff notes for the fixer: root
    # cause, the exact files/functions to change, suggested approach.
    findings: str = ""
    # Populated only for "proceed". Repo names (a directory name under
    # the workspace — the primary checkout and/or entries in
    # ctx.related_repos), naming every repo that needs a code change. A
    # Sentry URL carries no repo identity of its own, so this is the
    # authoritative answer to "where does this bug live".
    target_repos: list[str] = Field(default_factory=list)
    existing_pr_url: str | None = None
    commit_sha: str | None = None
    # Which LLM the fixer runs on (#43). Only meaningful when the run offers
    # a choice; the defaults keep the run's own credential and model.
    fixer_llm_credential: str = ""
    fixer_llm_tier: Literal["high", "heavy"] = "high"
    fixer_llm_reason: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def actionable(self) -> bool:
        """Serialized alongside ``kind`` for the backend, which persists
        per-issue triage verdicts as a two-state ACTIONABLE /
        NOT_ACTIONABLE enum (see api/plugins/sentry/consumer.py)."""
        return self.kind == "proceed"


class TriagedIssue(BaseModel):
    """One issue paired with its triage decision."""

    issue_id: str
    sentry_url: str
    triage: TriageOutput
    # Resolved by filter_and_route: triage's target_repos narrowed to the
    # repos actually checked out for this run. Empty means nothing
    # resolved, which is what stops the issue from being routed to a fix.
    target_repos: list[str] = Field(default_factory=list)


class SynthesisGroup(BaseModel):
    """A set of issues sharing a root cause."""

    issue_ids: list[str]
    root_cause: str
    # Every repo the group's fix touches — one draft PR/MR each. Always
    # recomputed from the member issues rather than taken from the
    # synthesis agent's own JSON (see synthesized_groups).
    target_repos: list[str] = Field(default_factory=list)
    sentry_urls: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    category: str = ""
    confidence: float = 0.0
    # Concatenated triage findings for every issue in the group — the
    # fixer's brief. Synthesis only decides grouping; it never rewrites
    # these.
    findings: str = ""


class RoutingPlan(BaseModel):
    """Deterministic decision after triage."""

    action: Literal["stop", "fix", "synthesize"]
    reason: str = ""
    groups: list[SynthesisGroup] = Field(default_factory=list)
    actionable: list[TriagedIssue] = Field(default_factory=list)
    triages: dict[str, TriagedIssue] = Field(default_factory=dict)
    # Issues triage marked "proceed" that were dropped anyway because none
    # of their target repos is checked out here. Never silent: the workflow
    # surfaces these as a warning rather than reporting a clean no-op.
    unresolved_repo: list[TriagedIssue] = Field(default_factory=list)
    # Sentry issue ids whose triage agent returned no usable structured
    # output. Not a verdict — the agent may have investigated and had its
    # answer lost on the way back — so these are reported as a failure
    # rather than folded in as "not actionable".
    unparsed: list[str] = Field(default_factory=list)


class GroupResult(BaseModel):
    """Outcome of running one synthesis group end-to-end."""

    group: SynthesisGroup
    # One entry per target repo the fixer actually pushed to, keyed by repo
    # name: {"pr_url", "pr_number", "provider", "head_branch", "ci"}. The
    # backend keys its execution↔PR link on pr_number + provider (no URL or
    # branch parsing). A repo triage listed but the fixer left untouched has
    # its draft closed and no entry here.
    repos: dict[str, dict[str, str | int]] = Field(default_factory=dict)
    ok: bool = True
    error: str = ""

    @property
    def pr_urls(self) -> list[str]:
        return [str(r["pr_url"]) for r in self.repos.values() if r.get("pr_url")]


class SynthesisAgentOutput(BaseModel):
    """JSON the synthesis agent emits."""

    groups: list[SynthesisGroup] = Field(default_factory=list)
