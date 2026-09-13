"""Direct unit coverage for issue-resolve's pure helpers — resolve_target_repos
in particular, since the workflow-level tests only exercise it indirectly."""

from __future__ import annotations

from pathlib import Path

from src.activities.git import resolve_target_repos
from src.activities.issue.schemas import IssueContext
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.workflows.issue_resolve.utils import pr_body, pr_title

_ISSUE_URL = "https://github.com/org/repo/issues/42"


def _ctx(related_repos: list[dict[str, str]] | None = None) -> RunContext:
    return RunContext(
        cwd=Path("/tmp/primary-repo"),
        workspace=Path("/tmp"),
        events=EventBus(),
        related_repos=related_repos or [],
    )


def _primary_ctx() -> RunContext:
    return RunContext(cwd=Path("/tmp/primary-repo"), workspace=Path("/tmp"), events=EventBus())


def _issue_ctx() -> IssueContext:
    return IssueContext(
        issue_url=_ISSUE_URL,
        provider="github",
        repo="org/repo",
        issue_number="42",
        issue_title="Widget crashes",
        issue_body="",
        comments="",
    )


# ── resolve_target_repos ────────────────────────────────────────────────


def test_resolve_target_repos_empty_falls_back_to_primary() -> None:
    """triage returning an empty list (e.g. a malformed output) must never
    leave the run with zero targets — fall back to the primary alone."""
    primary = _primary_ctx()
    resolved = resolve_target_repos(_ctx(), primary, [])
    assert resolved == [("primary-repo", primary)]


def test_resolve_target_repos_matches_related_repo_only() -> None:
    """A fix that lives entirely in a related repo — primary omitted from
    target_repos — must NOT get a worktree of its own."""
    ctx = _ctx(related_repos=[{"name": "backend-repo", "path": "/tmp/backend-repo"}])
    resolved = resolve_target_repos(ctx, _primary_ctx(), ["backend-repo"])
    names = [name for name, _ in resolved]
    assert names == ["backend-repo"]
    assert resolved[0][1].cwd == Path("/tmp/backend-repo")


def test_resolve_target_repos_includes_primary_when_named() -> None:
    ctx = _ctx(related_repos=[{"name": "backend-repo", "path": "/tmp/backend-repo"}])
    primary = _primary_ctx()
    resolved = resolve_target_repos(ctx, primary, ["primary-repo", "backend-repo"])
    names = [name for name, _ in resolved]
    assert names == ["primary-repo", "backend-repo"]


def test_resolve_target_repos_drops_unmatched_name_and_falls_back() -> None:
    ctx = _ctx(related_repos=[{"name": "backend-repo", "path": "/tmp/backend-repo"}])
    primary = _primary_ctx()
    resolved = resolve_target_repos(ctx, primary, ["nonexistent-repo"])
    assert resolved == [("primary-repo", primary)]


def test_resolve_target_repos_mixes_matched_and_unmatched_names() -> None:
    ctx = _ctx(
        related_repos=[
            {"name": "backend-repo", "path": "/tmp/backend-repo"},
            {"name": "frontend-repo", "path": "/tmp/frontend-repo"},
        ]
    )
    resolved = resolve_target_repos(
        ctx, _primary_ctx(), ["bogus-repo", "backend-repo", "also-bogus"]
    )
    names = [name for name, _ in resolved]
    assert names == ["backend-repo"]


def test_resolve_target_repos_dedupes_repeated_name() -> None:
    ctx = _ctx(related_repos=[{"name": "backend-repo", "path": "/tmp/backend-repo"}])
    resolved = resolve_target_repos(ctx, _primary_ctx(), ["backend-repo", "backend-repo"])
    names = [name for name, _ in resolved]
    assert names == ["backend-repo"]


def test_resolve_target_repos_primary_wins_name_collision() -> None:
    """If a related repo happens to share the primary's name (shouldn't
    normally happen, but garbage in shouldn't resolve to the wrong repo),
    the primary's own RunContext must win, not the related repo's."""
    ctx = _ctx(related_repos=[{"name": "primary-repo", "path": "/tmp/some-other-clone"}])
    primary = _primary_ctx()
    resolved = resolve_target_repos(ctx, primary, ["primary-repo"])
    assert resolved == [("primary-repo", primary)]


# ── pr_title / pr_body ──────────────────────────────────────────────────


def test_pr_title_without_repo_name() -> None:
    assert pr_title(_issue_ctx()) == "fix: Widget crashes"


def test_pr_title_with_repo_name_suffixes_it() -> None:
    assert pr_title(_issue_ctx(), "backend-repo") == "fix: Widget crashes (backend-repo)"


def test_pr_body_without_siblings_has_no_multi_repo_note() -> None:
    body = pr_body(_ISSUE_URL, _issue_ctx())
    assert f"Resolves {_ISSUE_URL}" in body
    assert "multi-repo" not in body


def test_pr_body_with_siblings_names_them() -> None:
    body = pr_body(_ISSUE_URL, _issue_ctx(), ["backend-repo", "frontend-repo"])
    assert "backend-repo" in body
    assert "frontend-repo" in body
    assert "multi-repo fix" in body
