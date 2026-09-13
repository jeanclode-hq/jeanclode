"""IssueResolveWorkflow — end-to-end with mocked agents and activities."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.ci_watch import CiWatchResult
from src.activities.issue.schemas import IssueContext
from src.activities.sentry import PRRef, WorktreePath
from src.agents.issue.schemas import TriageOutput
from src.agents.schemas import AgentResult
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.issue_resolve.runner import IssueResolveWorkflow
from src.workflows.issue_resolve.utils import issue_branch

_FAKE_REPO_DIR = Path("/tmp/fake-repo")
_PRIMARY_NAME = _FAKE_REPO_DIR.name  # "fake-repo" — what resolve_target_repos names it

GITHUB_ISSUE_URL = "https://github.com/org/repo/issues/42"
GITLAB_ISSUE_URL = "https://gitlab.com/group/project/-/issues/7"

# Deterministic branch for GITHUB_ISSUE_URL — single-repo worktrees are flat
# (ctx.workspace/"worktrees"/<branch slug>), not nested by repo name, so
# that's what ctx.cwd.name is for a single-repo run's PR/fixer-input checks.
_BRANCH_SLUG = issue_branch("42", GITHUB_ISSUE_URL).replace("/", "_")


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        issues=[GITHUB_ISSUE_URL],
    )


def _issue_ctx(provider: str = "github") -> IssueContext:
    return IssueContext(
        issue_url=GITHUB_ISSUE_URL if provider == "github" else GITLAB_ISSUE_URL,
        provider=provider,
        repo="org/repo" if provider == "github" else "gitlab.com/group/project",
        issue_number="42" if provider == "github" else "7",
        issue_title="Widget crashes on startup",
        issue_body="The widget crashes.",
        comments="**alice:** Confirmed.",
    )


def _triage(
    kind: str = "proceed",
    comment: str = "",
    target_repos: list[str] | None = None,
    findings: str = "",
) -> AgentResult:
    return AgentResult(
        text="",
        structured=TriageOutput(
            kind=kind,
            reasoning=f"reason for {kind}",
            comment_body=comment,
            target_repos=target_repos or [],
            findings=findings,
        ).model_dump(),
    )


def _create_worktree_side(
    branch: str, *, ctx: RunContext, dest: Path | None = None
) -> WorktreePath:
    path = dest if dest is not None else (ctx.workspace / "wt" / branch.replace("/", "_"))
    return WorktreePath(path=path, branch=branch, placeholder_sha="base-sha")


def _open_pr_side(
    branch: str, title: str, body: str, platform: str, *, ctx: RunContext, draft: bool = True
) -> PRRef:
    # Repo name is baked into ctx.cwd by _create_worktree_side (dest ends in
    # the repo name for multi-repo, or is the flat single-repo worktree dir
    # otherwise) — use it so each repo's PR gets a distinguishable URL.
    return PRRef(
        url=f"https://github.com/org/repo/pull/{ctx.cwd.name}",
        branch=branch,
        platform=platform,  # type: ignore[arg-type]
    )


def _patch_workflow(
    *,
    triage: AgentResult,
    fixer: AgentResult | None = None,
    issue_ctx: IssueContext | None = None,
    post_comment_raises: bool = False,
    verify_fix_pushed_reason: str | None = None,
    ci_result: CiWatchResult | None = None,
) -> ExitStack:
    stack = ExitStack()

    ctx_value = issue_ctx or _issue_ctx()
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.fetch_issue_context",
            return_value=ctx_value,
        )
    )

    triage_mock = AsyncMock(return_value=triage)
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.TriageAgent",
            return_value=MagicMock(invoke=triage_mock),
        )
    )

    fixer_mock = AsyncMock(return_value=fixer or AgentResult(text=""))
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.IssueFixerAgent",
            return_value=MagicMock(invoke=fixer_mock),
        )
    )

    if post_comment_raises:
        stack.enter_context(
            patch(
                "src.workflows.issue_resolve.runner.post_issue_comment",
                side_effect=RuntimeError("comment failed"),
            )
        )
    else:
        stack.enter_context(patch("src.workflows.issue_resolve.runner.post_issue_comment"))

    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.ensure_repo",
            return_value=_FAKE_REPO_DIR,
        )
    )
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.create_worktree",
            side_effect=_create_worktree_side,
        )
    )
    # push_branch IS called by the runner itself (right after create_worktree,
    # to force-sync origin before the fixer starts), so it needs mocking here
    # even though the fixer's own commits are mocked away with IssueFixerAgent.
    stack.enter_context(patch("src.workflows.issue_resolve.runner.push_branch"))
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.open_pr",
            side_effect=_open_pr_side,
        )
    )
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.verify_fix_pushed",
            return_value=verify_fix_pushed_reason,
        )
    )
    stack.enter_context(patch("src.workflows.issue_resolve.runner.attach_label"))
    stack.enter_context(
        patch(
            "src.workflows.issue_resolve.runner.check_ci",
            return_value=ci_result
            or CiWatchResult(outcome="finish", reason="ok", summary_text="ok"),
        )
    )
    return stack


# ── Error / guard paths ────────────────────────────────────────────────────


async def test_returns_error_when_no_issue_url(tmp_path: Path) -> None:
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), issues=[])
    result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "error"
    assert "No issue URL" in result.summary


async def test_returns_error_for_unrecognised_url(tmp_path: Path) -> None:
    ctx = RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        issues=["https://sentry.io/issues/123"],
    )
    result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "error"
    assert "parse" in result.summary.lower()


async def test_returns_error_when_triage_returns_nothing(ctx: RunContext) -> None:
    bad_result = AgentResult(text="not json", structured=None)
    triage_mock = AsyncMock(return_value=bad_result)
    with (
        patch(
            "src.workflows.issue_resolve.runner.ensure_repo",
            return_value=_FAKE_REPO_DIR,
        ),
        patch(
            "src.workflows.issue_resolve.runner.fetch_issue_context",
            return_value=_issue_ctx(),
        ),
        patch(
            "src.workflows.issue_resolve.runner.TriageAgent",
            return_value=MagicMock(invoke=triage_mock),
        ),
    ):
        result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "error"
    assert "no valid output" in result.summary


# ── Non-proceed triage outcomes ───────────────────────────────────────────


@pytest.mark.parametrize(
    "kind",
    ["needs_info", "push_back", "duplicate", "already_fixed", "refuse", "split"],
)
async def test_non_proceed_outcomes_post_comment_and_stop(ctx: RunContext, kind: str) -> None:
    comment = f"comment for {kind}"
    with _patch_workflow(triage=_triage(kind=kind, comment=comment)):
        result = await IssueResolveWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["kind"] == kind
    assert result.data["triage_result"] == "not_actionable"


async def test_non_proceed_outcome_comment_failure_does_not_crash(
    ctx: RunContext,
) -> None:
    with _patch_workflow(
        triage=_triage(kind="refuse", comment="won't do it"),
        post_comment_raises=True,
    ):
        result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "success"
    assert result.data["kind"] == "refuse"


async def test_non_proceed_emits_triage_panel(ctx: RunContext) -> None:
    seen: list[Panel] = []
    ctx.events.subscribe(lambda e: seen.append(e) if isinstance(e, Panel) else None)
    with _patch_workflow(triage=_triage(kind="duplicate", comment="see #1")):
        await IssueResolveWorkflow().run(ctx)
    triage_panel = next((p for p in seen if p.title == "Triage Results"), None)
    assert triage_panel is not None
    assert "duplicate" in triage_panel.content


# ── proceed path (single repo) ─────────────────────────────────────────────


async def test_proceed_path_opens_pr_and_labels(ctx: RunContext) -> None:
    with _patch_workflow(triage=_triage(kind="proceed")):
        result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "success"
    assert result.data["triage_result"] == "actionable"
    assert result.data["pr_urls"] == [f"https://github.com/org/repo/pull/{_BRANCH_SLUG}"]


async def test_proceed_path_errors_when_fixer_never_pushed(ctx: RunContext) -> None:
    with _patch_workflow(
        triage=_triage(kind="proceed"),
        verify_fix_pushed_reason="no code changes have been committed yet",
    ):
        result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "error"
    assert "without pushing" in result.summary
    assert result.data["pr_urls"] == []


async def test_proceed_path_labels_and_succeeds_when_ci_fails(ctx: RunContext) -> None:
    # A real, pushed fix always gets its PR labeled and the run reports
    # success — the CI gate hook already gave the fixer its retry budget to
    # fix anything it broke; a failure that survives that isn't a run
    # failure, just something a human can see via the recorded "ci" field.
    with (
        _patch_workflow(
            triage=_triage(kind="proceed"),
            ci_result=CiWatchResult(
                outcome="failure", reason="required check(s) failed: test", summary_text="red"
            ),
        ),
        patch("src.workflows.issue_resolve.runner.attach_label") as attach_label_mock,
    ):
        result = await IssueResolveWorkflow().run(ctx)
    assert result.status == "success"
    assert result.data["repos"][_PRIMARY_NAME]["ci"] == "failure"
    assert result.data["pr_urls"] == [f"https://github.com/org/repo/pull/{_BRANCH_SLUG}"]
    assert attach_label_mock.call_count == 2


async def test_proceed_path_emits_pr_opened_panel(ctx: RunContext) -> None:
    seen: list[Panel] = []
    ctx.events.subscribe(lambda e: seen.append(e) if isinstance(e, Panel) else None)
    with _patch_workflow(triage=_triage(kind="proceed")):
        await IssueResolveWorkflow().run(ctx)
    titles = [p.title for p in seen]
    assert "PR Opened" in titles


async def test_proceed_invokes_fixer_with_triage_findings(ctx: RunContext) -> None:
    """No planner stage anymore — the fixer runs straight off triage's findings."""
    fixer_calls: list[Any] = []

    async def fixer_side(inp: Any, run_ctx: Any, **_kwargs: Any) -> AgentResult:
        fixer_calls.append(inp)
        return AgentResult(text="")

    fixer_mock = AsyncMock(side_effect=fixer_side)

    with (
        _patch_workflow(triage=_triage(kind="proceed", findings="root cause is in widget.py:42")),
        patch(
            "src.workflows.issue_resolve.runner.IssueFixerAgent",
            return_value=MagicMock(invoke=fixer_mock),
        ),
    ):
        await IssueResolveWorkflow().run(ctx)

    assert len(fixer_calls) == 1
    assert fixer_calls[0].issue_url == GITHUB_ISSUE_URL
    assert fixer_calls[0].findings == "root cause is in widget.py:42"
    expected_path = str(ctx.workspace / "worktrees" / _BRANCH_SLUG)
    assert fixer_calls[0].repos == [
        {
            "name": _PRIMARY_NAME,
            "path": expected_path,
            "ci_bypass_path": f"{expected_path}.ci-bypass",
        }
    ]


# ── target_repos routing ────────────────────────────────────────────────


async def test_proceed_with_unmatched_target_repo_only_uses_primary(
    ctx: RunContext,
) -> None:
    """A target_repos entry triage names but that wasn't actually cloned is
    dropped. With nothing left resolved, resolve_target_repos falls back to
    the primary alone, so the run still proceeds against just that."""
    worktree_calls: list[Any] = []

    def create_worktree_side(branch: str, *, ctx: Any, dest: Any = None) -> WorktreePath:
        worktree_calls.append(ctx.cwd)
        return _create_worktree_side(branch, ctx=ctx, dest=dest)

    with (
        _patch_workflow(triage=_triage(kind="proceed", target_repos=["nonexistent-repo"])),
        patch(
            "src.workflows.issue_resolve.runner.create_worktree",
            side_effect=create_worktree_side,
        ),
    ):
        result = await IssueResolveWorkflow().run(ctx)

    assert result.status == "success"
    assert worktree_calls == [_FAKE_REPO_DIR]
    assert result.data["pr_urls"] == [f"https://github.com/org/repo/pull/{_BRANCH_SLUG}"]


# ── multi-repo fixes ────────────────────────────────────────────────────


def _multi_repo_ctx(tmp_path: Path, related_name: str = "backend-repo") -> RunContext:
    related_path = tmp_path / "related-checkout"
    related_path.mkdir()
    return RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        issues=[GITHUB_ISSUE_URL],
        related_repos=[{"name": related_name, "path": str(related_path)}],
    )


async def test_multi_repo_creates_a_worktree_per_target_repo(tmp_path: Path) -> None:
    multi_ctx = _multi_repo_ctx(tmp_path)
    worktree_calls: list[tuple[Path, Path | None]] = []

    def create_worktree_side(branch: str, *, ctx: Any, dest: Any = None) -> WorktreePath:
        worktree_calls.append((ctx.cwd, dest))
        return _create_worktree_side(branch, ctx=ctx, dest=dest)

    with (
        _patch_workflow(
            triage=_triage(
                kind="proceed",
                target_repos=[_PRIMARY_NAME, "backend-repo"],
                findings="spans both",
            )
        ),
        patch(
            "src.workflows.issue_resolve.runner.create_worktree",
            side_effect=create_worktree_side,
        ),
    ):
        result = await IssueResolveWorkflow().run(multi_ctx)

    assert result.status == "success"
    assert {c[0] for c in worktree_calls} == {_FAKE_REPO_DIR, tmp_path / "related-checkout"}
    # Multi-repo worktrees nest under one shared parent, named per repo.
    dests = {d.name for _, d in worktree_calls if d is not None}
    assert dests == {_PRIMARY_NAME, "backend-repo"}
    assert len({d.parent for _, d in worktree_calls if d is not None}) == 1
    assert sorted(result.data["pr_urls"]) == sorted(
        [
            f"https://github.com/org/repo/pull/{_PRIMARY_NAME}",
            "https://github.com/org/repo/pull/backend-repo",
        ]
    )


async def test_multi_repo_fix_entirely_in_related_repo_skips_primary_worktree(
    tmp_path: Path,
) -> None:
    """When triage's target_repos names only a related repo, the primary
    must get no worktree at all — critical when the primary is an empty
    scaffold repo that can't even branch off (see jc-gitlab-c4967424)."""
    multi_ctx = _multi_repo_ctx(tmp_path)
    worktree_calls: list[Path] = []

    def create_worktree_side(branch: str, *, ctx: Any, dest: Any = None) -> WorktreePath:
        worktree_calls.append(ctx.cwd)
        return _create_worktree_side(branch, ctx=ctx, dest=dest)

    with (
        _patch_workflow(triage=_triage(kind="proceed", target_repos=["backend-repo"])),
        patch(
            "src.workflows.issue_resolve.runner.create_worktree",
            side_effect=create_worktree_side,
        ),
    ):
        result = await IssueResolveWorkflow().run(multi_ctx)

    assert result.status == "success"
    assert worktree_calls == [tmp_path / "related-checkout"]
    # A single resolved target behaves like any other single-repo run: flat
    # worktree layout, PR URL keyed off the branch slug rather than a repo
    # name (see the module-level _BRANCH_SLUG comment).
    assert result.data["pr_urls"] == [f"https://github.com/org/repo/pull/{_BRANCH_SLUG}"]


async def test_multi_repo_gives_fixer_both_worktree_paths(tmp_path: Path) -> None:
    multi_ctx = _multi_repo_ctx(tmp_path)
    fixer_calls: list[Any] = []

    async def fixer_side(inp: Any, run_ctx: Any, **_kwargs: Any) -> AgentResult:
        fixer_calls.append(inp)
        return AgentResult(text="")

    with (
        _patch_workflow(
            triage=_triage(kind="proceed", target_repos=[_PRIMARY_NAME, "backend-repo"])
        ),
        patch(
            "src.workflows.issue_resolve.runner.IssueFixerAgent",
            return_value=MagicMock(invoke=AsyncMock(side_effect=fixer_side)),
        ),
    ):
        await IssueResolveWorkflow().run(multi_ctx)

    assert len(fixer_calls) == 1
    names = {r["name"] for r in fixer_calls[0].repos}
    assert names == {_PRIMARY_NAME, "backend-repo"}


async def test_multi_repo_untouched_target_gets_no_pr(tmp_path: Path) -> None:
    """Triage names a repo that turns out not to need a change — the fixer
    never pushes there, so it gets no PR and no error, only the repo that
    did get a real push does."""
    multi_ctx = _multi_repo_ctx(tmp_path)

    def verify_fix_pushed_side(branch: str, placeholder_sha: str, *, ctx: Any) -> str | None:
        if ctx.cwd.name == "backend-repo":
            return None  # pushed
        return "no code changes have been committed yet"  # primary untouched

    with (
        _patch_workflow(
            triage=_triage(kind="proceed", target_repos=[_PRIMARY_NAME, "backend-repo"])
        ),
        patch(
            "src.workflows.issue_resolve.runner.verify_fix_pushed",
            side_effect=verify_fix_pushed_side,
        ),
    ):
        result = await IssueResolveWorkflow().run(multi_ctx)

    assert result.status == "success"
    assert result.data["pr_urls"] == ["https://github.com/org/repo/pull/backend-repo"]
    assert _PRIMARY_NAME not in result.data["repos"]


async def test_multi_repo_one_repo_ci_failure_still_labels_both(tmp_path: Path) -> None:
    """Both repos got a real, pushed fix; only one's CI is green — both still
    get their PR labeled and the run still succeeds, since labeling and
    run status don't depend on the CI result."""
    multi_ctx = _multi_repo_ctx(tmp_path)

    def check_ci_side(pr: PRRef, *, cwd: Path) -> CiWatchResult:
        if cwd.name == "backend-repo":
            return CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
        return CiWatchResult(
            outcome="failure", reason="required check(s) failed: test", summary_text="red"
        )

    with (
        _patch_workflow(
            triage=_triage(kind="proceed", target_repos=[_PRIMARY_NAME, "backend-repo"])
        ),
        patch(
            "src.workflows.issue_resolve.runner.check_ci",
            side_effect=check_ci_side,
        ),
    ):
        result = await IssueResolveWorkflow().run(multi_ctx)

    assert result.status == "success"
    assert result.data["repos"][_PRIMARY_NAME]["ci"] == "failure"
    assert result.data["repos"]["backend-repo"]["ci"] == "finish"
    assert set(result.data["pr_urls"]) == {
        "https://github.com/org/repo/pull/backend-repo",
        f"https://github.com/org/repo/pull/{_PRIMARY_NAME}",
    }


# ── GitLab path ───────────────────────────────────────────────────────────


async def test_proceed_path_works_for_gitlab(tmp_path: Path) -> None:
    gl_ctx = RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        issues=[GITLAB_ISSUE_URL],
    )
    with _patch_workflow(
        triage=_triage(kind="proceed"),
        issue_ctx=_issue_ctx(provider="gitlab"),
    ):
        # open_pr mock always returns a github PRRef — provider is set in the real call
        result = await IssueResolveWorkflow().run(gl_ctx)
    assert result.status == "success"
    assert result.data["triage_result"] == "actionable"


# ── Workflow registration ─────────────────────────────────────────────────


def test_workflow_is_registered() -> None:
    from src.workflows import WORKFLOWS

    assert "issue-resolve" in WORKFLOWS


def test_workflow_triggers_contain_command() -> None:
    assert "command:issue-resolve" in IssueResolveWorkflow.triggers


def test_workflow_triggers_match_github_issue_url() -> None:
    import fnmatch

    from src.workflows.utils import strip_url_scheme

    normalized = strip_url_scheme(GITHUB_ISSUE_URL)
    assert any(
        fnmatch.fnmatch(normalized, t)
        for t in IssueResolveWorkflow.triggers
        if not t.startswith("command:")
    )


def test_workflow_triggers_match_self_hosted_gitlab_urls() -> None:
    """Self-hosted instances are almost always "gitlab.<tld>" (gitlab as the
    *first* hostname label, e.g. gitlab.example.dev) — "*.gitlab.*" requires
    a literal "." before "gitlab" and never matches that."""
    import fnmatch

    from src.workflows.utils import strip_url_scheme

    urls = [
        "https://gitlab.example.dev/jdoe/webshop/-/issues/10",
        "https://gitlab.example.dev/jdoe/webshop/-/work_items/10",
    ]
    for url in urls:
        normalized = strip_url_scheme(url)
        assert any(
            fnmatch.fnmatch(normalized, t)
            for t in IssueResolveWorkflow.triggers
            if not t.startswith("command:")
        ), url
