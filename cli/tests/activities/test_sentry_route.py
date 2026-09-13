"""filter_and_route — deterministic stop / fix / synthesize routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.activities.sentry import (
    RoutingPlan,
    TriagedIssue,
    TriageOutput,
    filter_and_route,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _checkout(root: Path, name: str) -> Path:
    """A directory that looks like a git checkout to resolve_target_repos."""
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    """cwd is a real checkout named "repo", as preflight leaves it."""
    return RunContext(cwd=_checkout(tmp_path, "repo"), workspace=tmp_path, events=EventBus())


def _triaged(
    issue_id: str,
    *,
    kind: str = "proceed",
    target_repos: list[str] | None = None,
    existing_pr_url: str | None = None,
    root_cause: str = "missing null check",
    findings: str = "guard the None case",
    confidence: float = 0.9,
) -> TriagedIssue:
    return TriagedIssue(
        issue_id=issue_id,
        sentry_url=f"https://sentry.io/issues/{issue_id}",
        triage=TriageOutput(
            kind=kind,  # type: ignore[arg-type]
            confidence=confidence,
            root_cause_hypothesis=root_cause,
            findings=findings,
            existing_pr_url=existing_pr_url,
            target_repos=["repo"] if target_repos is None else target_repos,
            affected_files=["src/x.py"],
        ),
    )


def test_stops_when_nothing_actionable(ctx: RunContext) -> None:
    plan = filter_and_route([_triaged("1", kind="not_actionable")], ctx=ctx)
    assert plan.action == "stop"
    assert "1" in plan.triages
    assert plan.unresolved_repo == []


def test_stops_when_existing_pr(ctx: RunContext) -> None:
    plan = filter_and_route(
        [_triaged("1", kind="duplicate", existing_pr_url="https://github.com/org/repo/pull/42")],
        ctx=ctx,
    )
    assert plan.action == "stop"


def test_stops_when_previously_rejected(ctx: RunContext) -> None:
    """A closed-unmerged fix is a human saying no — never re-propose it."""
    plan = filter_and_route(
        [
            _triaged(
                "1",
                kind="previously_rejected",
                existing_pr_url="https://gitlab.example/org/repo/-/merge_requests/625",
            )
        ],
        ctx=ctx,
    )
    assert plan.action == "stop"


def test_target_repo_not_checked_out_is_reported_not_silent(ctx: RunContext) -> None:
    plan = filter_and_route([_triaged("1", target_repos=["some-other-repo"])], ctx=ctx)
    assert plan.action == "stop"
    assert [t.issue_id for t in plan.unresolved_repo] == ["1"]
    assert "no target repo" in plan.reason.lower()


def test_empty_target_repos_falls_back_to_the_checkout_on_disk(ctx: RunContext) -> None:
    """The regression that made every self-hosted run a no-op: triage left
    the repo field null, and routing dropped an otherwise-actionable issue
    even though the repo was cloned and sitting in cwd."""
    plan = filter_and_route([_triaged("1", target_repos=[])], ctx=ctx)
    assert plan.action == "fix"
    assert plan.groups[0].target_repos == ["repo"]


def test_no_checkout_at_all_reports_unresolved(tmp_path: Path) -> None:
    bare = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())
    plan = filter_and_route([_triaged("1", target_repos=[])], ctx=bare)
    assert plan.action == "stop"
    assert [t.issue_id for t in plan.unresolved_repo] == ["1"]


def test_single_actionable_picks_fix(ctx: RunContext) -> None:
    plan = filter_and_route([_triaged("1")], ctx=ctx)
    assert plan.action == "fix"
    assert len(plan.groups) == 1
    assert plan.groups[0].issue_ids == ["1"]
    assert plan.groups[0].root_cause == "missing null check"
    assert plan.groups[0].target_repos == ["repo"]
    assert plan.groups[0].sentry_urls == ["https://sentry.io/issues/1"]
    assert "guard the None case" in plan.groups[0].findings


def test_multiple_actionable_picks_synthesize(ctx: RunContext) -> None:
    plan = filter_and_route([_triaged("1"), _triaged("2")], ctx=ctx)
    assert plan.action == "synthesize"
    assert len(plan.actionable) == 2


def test_target_repo_can_be_a_related_repo(tmp_path: Path) -> None:
    """Triage may find the bug originates in a sibling checkout, not the
    repo it started in."""
    primary = _checkout(tmp_path, "webshop")
    sibling = _checkout(tmp_path, "segment-api")
    ctx = RunContext(
        cwd=primary,
        workspace=tmp_path,
        events=EventBus(),
        related_repos=[{"name": "segment-api", "path": str(sibling)}],
    )
    plan = filter_and_route([_triaged("1", target_repos=["segment-api"])], ctx=ctx)
    assert plan.action == "fix"
    assert plan.groups[0].target_repos == ["segment-api"]


def test_target_repos_can_span_two_checkouts(tmp_path: Path) -> None:
    primary = _checkout(tmp_path, "webshop")
    sibling = _checkout(tmp_path, "segment-api")
    ctx = RunContext(
        cwd=primary,
        workspace=tmp_path,
        events=EventBus(),
        related_repos=[{"name": "segment-api", "path": str(sibling)}],
    )
    plan = filter_and_route([_triaged("1", target_repos=["webshop", "segment-api"])], ctx=ctx)
    assert plan.action == "fix"
    assert plan.groups[0].target_repos == ["webshop", "segment-api"]


def test_unknown_names_are_dropped_but_known_ones_survive(tmp_path: Path) -> None:
    primary = _checkout(tmp_path, "webshop")
    ctx = RunContext(cwd=primary, workspace=tmp_path, events=EventBus())
    plan = filter_and_route([_triaged("1", target_repos=["webshop", "ghost-repo"])], ctx=ctx)
    assert plan.action == "fix"
    assert plan.groups[0].target_repos == ["webshop"]


def test_returns_pydantic_routing_plan(ctx: RunContext) -> None:
    plan = filter_and_route([], ctx=ctx)
    assert isinstance(plan, RoutingPlan)
