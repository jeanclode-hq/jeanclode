"""SentryFixWorkflow — end-to-end with mocked agents and activities."""

from __future__ import annotations

import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.ci_watch import CiWatchResult
from src.activities.sentry import (
    PRRef,
    SentryEvent,
    SynthesisAgentOutput,
    SynthesisGroup,
    TriageOutput,
    WorktreePath,
)
from src.agents.schemas import AgentResult
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.base import find_workflow
from src.workflows.sentry_fix.runner import SentryFixWorkflow


def test_triggers_match_self_hosted_sentry_url() -> None:
    assert find_workflow(url="https://sentry.example.com/issues/1883122/") is SentryFixWorkflow


def test_triggers_match_saas_sentry_url() -> None:
    assert find_workflow(url="https://acme.sentry.io/issues/12345/") is SentryFixWorkflow


def _checkout(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    """cwd is the checkout preflight cloned for this run, named "repo"."""
    return RunContext(
        cwd=_checkout(tmp_path, "repo"),
        workspace=tmp_path,
        events=EventBus(),
        issues=["https://sentry.io/issues/1"],
    )


def _event(issue_id: str) -> SentryEvent:
    return SentryEvent(
        issue_id=issue_id,
        sentry_url=f"https://sentry.io/issues/{issue_id}",
        formatted=f"data for {issue_id}",
    )


def _triage(
    kind: str = "proceed",
    root_cause: str = "missing null",
    target_repos: list[str] | None = None,
) -> AgentResult:
    return AgentResult(
        text="",
        structured=TriageOutput(
            kind=kind,  # type: ignore[arg-type]
            confidence=0.9,
            root_cause_hypothesis=root_cause,
            findings=f"fix for {root_cause}",
            affected_files=["src/x.py"],
            category="null_reference",
            target_repos=["repo"] if target_repos is None else target_repos,
        ).model_dump(),
    )


def _synthesis(groups: list[SynthesisGroup]) -> AgentResult:
    return AgentResult(
        text="",
        structured=SynthesisAgentOutput(groups=groups).model_dump(),
    )


def _patch_workflow(
    *,
    triage: list[AgentResult],
    synthesis: AgentResult | None = None,
    fixer: AgentResult | None = None,
    fixer_mock: AsyncMock | None = None,
    fetch: list[SentryEvent] | None = None,
    open_pr_side_effect: Any = None,
    verify_fix_pushed_reason: str | None = None,
    verify_fix_pushed_side_effect: Any = None,
    ci_result: CiWatchResult | None = None,
) -> ExitStack:
    """Patch every dependency the workflow touches and return the stack."""
    stack = ExitStack()

    fetch_value = fetch if fetch is not None else [_event("1")]
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.fetch_sentry_data",
            return_value=fetch_value,
        )
    )

    triage_iter = iter(triage)
    triage_mock = AsyncMock(side_effect=lambda *_a, **_kw: next(triage_iter))
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.TriageAgent",
            return_value=MagicMock(invoke=triage_mock),
        )
    )

    synth_mock = AsyncMock(return_value=synthesis or _synthesis([]))
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.SynthesisAgent",
            return_value=MagicMock(invoke=synth_mock),
        )
    )

    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.FixerAgent",
            return_value=MagicMock(invoke=fixer_mock or AsyncMock(return_value=fixer)),
        )
    )

    stack.enter_context(patch("src.workflows.sentry_fix.runner.gitleaks_scan"))
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.ensure_repo",
            side_effect=lambda repo_url, *, ctx: ctx.workspace / "repo",
        )
    )
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.create_worktree",
            side_effect=lambda branch, *, ctx, dest=None: WorktreePath(
                path=dest or (ctx.workspace / "wt" / branch.replace("/", "_")),
                branch=branch,
            ),
        )
    )
    stack.enter_context(patch("src.workflows.sentry_fix.runner.push_branch"))
    if open_pr_side_effect is not None:
        stack.enter_context(
            patch(
                "src.workflows.sentry_fix.runner.open_pr",
                side_effect=open_pr_side_effect,
            )
        )
    else:
        stack.enter_context(
            patch(
                "src.workflows.sentry_fix.runner.open_pr",
                side_effect=lambda branch, title, body, platform, *, ctx: PRRef(
                    url=f"https://github.com/org/{ctx.cwd.name}/pull/{branch[-1]}",
                    branch=branch,
                    platform="github",
                ),
            )
        )
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.verify_fix_pushed",
            side_effect=verify_fix_pushed_side_effect,
            return_value=None if verify_fix_pushed_side_effect else verify_fix_pushed_reason,
        )
    )
    stack.enter_context(patch("src.workflows.sentry_fix.runner.close_pr"))
    stack.enter_context(patch("src.workflows.sentry_fix.runner.attach_label"))
    stack.enter_context(
        patch("src.workflows.sentry_fix.runner.detect_platform", return_value="github")
    )
    stack.enter_context(
        patch(
            "src.workflows.sentry_fix.runner.check_ci",
            return_value=ci_result
            or CiWatchResult(outcome="finish", reason="ok", summary_text="ok"),
        )
    )
    return stack


def _multi_ctx(tmp_path: Path) -> RunContext:
    """A run with a sibling checkout cloned alongside the primary."""
    return RunContext(
        cwd=_checkout(tmp_path, "webshop"),
        workspace=tmp_path,
        events=EventBus(),
        issues=["https://sentry.io/issues/1"],
        related_repos=[{"name": "segment-api", "path": str(_checkout(tmp_path, "segment-api"))}],
    )


async def test_returns_error_when_fetch_yields_nothing(ctx: RunContext) -> None:
    with _patch_workflow(triage=[], fetch=[]):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    assert "Failed to fetch" in result.summary


async def test_stop_path_when_no_actionable(ctx: RunContext) -> None:
    seen: list[Panel] = []
    ctx.events.subscribe(lambda e: seen.append(e) if isinstance(e, Panel) else None)
    with _patch_workflow(triage=[_triage(kind="not_actionable")]):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "success"
    assert result.data["action"] == "stop"
    triage_panel = next((p for p in seen if p.title == "Triage Results"), None)
    assert triage_panel is not None
    assert "not actionable" in triage_panel.content
    assert "stopping" in triage_panel.content.lower()


async def test_actionable_issue_with_no_resolvable_repo_is_an_error(ctx: RunContext) -> None:
    """The silent-no-op regression: triage says fix it, nothing gets fixed,
    and the run used to report success anyway (jc-sentry-1887773)."""
    with _patch_workflow(triage=[_triage(target_repos=["not-cloned-here"])]):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    assert result.data["action"] == "stop"
    assert result.data["unresolved_repo"] == ["1"]


async def test_empty_structured_output_is_a_failure_not_a_verdict(ctx: RunContext) -> None:
    """A missing payload used to validate to an all-defaults TriageOutput —
    kind="not_actionable", no reason — indistinguishable from a real verdict
    even though the agent may have done the whole investigation."""
    seen: list[Panel] = []
    ctx.events.subscribe(lambda e: seen.append(e) if isinstance(e, Panel) else None)
    with _patch_workflow(triage=[AgentResult(text="", structured=None)]):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    assert result.data["unparsed"] == ["1"]
    assert "no usable output" in result.summary
    panel = next(p for p in seen if p.title == "Triage Results")
    assert "1" in panel.content


async def test_unrecognised_structured_output_is_a_failure(ctx: RunContext) -> None:
    """Keys the schema doesn't know are dropped by extra="ignore", leaving
    every field at its default — so "nothing matched" must be caught before
    it reads as a verdict."""
    with _patch_workflow(
        triage=[AgentResult(text="", structured={"verdict": "fix it", "repo": "webshop"})]
    ):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    assert result.data["unparsed"] == ["1"]


async def test_one_unusable_triage_does_not_sink_the_others(ctx: RunContext) -> None:
    ctx.issues.append("https://sentry.io/issues/2")
    with _patch_workflow(
        triage=[AgentResult(text="", structured=None), _triage()],
        fetch=[_event("1"), _event("2")],
    ):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "success"
    assert len(result.data["pr_urls"]) == 1
    assert result.data["unparsed"] == ["1"]
    assert "returned nothing usable" in result.summary


async def test_fix_path_emits_pr_panel(ctx: RunContext) -> None:
    seen: list[Panel] = []
    ctx.events.subscribe(lambda e: seen.append(e) if isinstance(e, Panel) else None)
    with _patch_workflow(triage=[_triage()]):
        await SentryFixWorkflow().run(ctx)
    titles = [p.title for p in seen]
    assert "PR Opened" in titles


async def test_single_actionable_runs_fix_path(ctx: RunContext) -> None:
    ctx.issues.append("https://sentry.io/issues/2")
    with _patch_workflow(
        triage=[_triage(), _triage(kind="not_actionable")],
        fetch=[_event("1"), _event("2")],
    ):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "success"
    assert result.data["action"] == "fix"
    assert len(result.data["pr_urls"]) == 1


async def test_group_result_repos_carry_pr_number_and_provider(ctx: RunContext) -> None:
    """Part D2: the backend keys its execution↔PR link on pr_number + provider,
    so the structured result must carry both per repo — no URL/branch parsing."""
    with _patch_workflow(triage=[_triage()]):
        result = await SentryFixWorkflow().run(ctx)
    repo_entry = result.data["results"][0]["repos"]["repo"]
    assert repo_entry["pr_number"] == int(repo_entry["pr_url"].rstrip("/").rsplit("/", 1)[-1])
    assert repo_entry["provider"] == "github"
    assert re.fullmatch(r"fix/1\d{4}-sentry-[0-9a-f]{7}", repo_entry["head_branch"])


async def test_fixer_gets_triage_findings_not_a_separate_plan(ctx: RunContext) -> None:
    """There is no planner stage — triage's findings are the fixer's brief."""
    fixer_mock = AsyncMock(return_value=AgentResult(text=""))
    with _patch_workflow(triage=[_triage(root_cause="stale category")], fixer_mock=fixer_mock):
        await SentryFixWorkflow().run(ctx)
    fixer_input = fixer_mock.call_args.args[0]
    assert "fix for stale category" in fixer_input.findings
    assert [r["name"] for r in fixer_input.repos] == ["repo"]


async def test_group_result_repos_carry_identifiers_on_fixer_exception(ctx: RunContext) -> None:
    """Even when the fixer blows up after the PR is open, the repos entry still
    carries pr_number + provider so the backend can track the leftover PR."""
    fixer_mock = AsyncMock(side_effect=RuntimeError("fixer exploded"))
    with _patch_workflow(triage=[_triage()], fixer_mock=fixer_mock):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    entry = result.data["results"][0]["repos"]["repo"]
    assert entry["provider"] == "github"
    assert entry["pr_number"] == int(entry["pr_url"].rstrip("/").rsplit("/", 1)[-1])
    assert "ci" not in entry


async def test_fix_path_fails_group_when_fixer_never_pushed(ctx: RunContext) -> None:
    with _patch_workflow(
        triage=[_triage()],
        verify_fix_pushed_reason="no code changes have been committed yet",
    ):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "error"
    assert result.data["pr_urls"] == []
    error_results = [r for r in result.data["results"] if not r["ok"]]
    assert len(error_results) == 1
    assert "without pushing" in error_results[0]["error"]


# ── multi-repo groups ───────────────────────────────────────────────────


async def test_multi_repo_opens_one_draft_per_target_before_the_fixer_runs(
    tmp_path: Path,
) -> None:
    ctx = _multi_ctx(tmp_path)
    opened: list[tuple[str, str, Path]] = []

    def open_pr(branch: str, title: str, body: str, platform: str, *, ctx: RunContext) -> PRRef:
        opened.append((branch, title, ctx.cwd))
        return PRRef(url=f"https://git/{ctx.cwd.name}/1", branch=branch, platform="github")

    fixer_mock = AsyncMock(return_value=AgentResult(text=""))
    with _patch_workflow(
        triage=[_triage(target_repos=["webshop", "segment-api"])],
        open_pr_side_effect=open_pr,
        fixer_mock=fixer_mock,
    ):
        result = await SentryFixWorkflow().run(ctx)

    assert result.status == "success"
    assert len(opened) == 2
    # Both drafts exist before the fixer is invoked.
    assert fixer_mock.await_count == 1
    assert len(result.data["pr_urls"]) == 2


async def test_every_pr_is_opened_from_its_own_worktree_on_the_group_branch(
    tmp_path: Path,
) -> None:
    """The duplicate-MR bug: `glab mr create` takes its source branch from
    the cwd, so opening a PR from anywhere but that repo's worktree points
    the MR at the wrong branch."""
    ctx = _multi_ctx(tmp_path)
    opened: list[tuple[str, Path]] = []
    worktrees: dict[str, Path] = {}

    def make_worktree(branch: str, *, ctx: RunContext, dest: Path | None = None) -> WorktreePath:
        path = dest or (ctx.workspace / "wt" / branch.replace("/", "_"))
        worktrees[ctx.cwd.name] = path
        return WorktreePath(path=path, branch=branch)

    def open_pr(branch: str, title: str, body: str, platform: str, *, ctx: RunContext) -> PRRef:
        opened.append((branch, ctx.cwd))
        return PRRef(url=f"https://git/{ctx.cwd.name}/1", branch=branch, platform="github")

    with (
        _patch_workflow(
            triage=[_triage(target_repos=["webshop", "segment-api"])],
            open_pr_side_effect=open_pr,
        ),
        patch("src.workflows.sentry_fix.runner.create_worktree", side_effect=make_worktree),
    ):
        await SentryFixWorkflow().run(ctx)

    branches = {branch for branch, _ in opened}
    assert len(branches) == 1  # one branch for the whole group
    assert {cwd for _, cwd in opened} == set(worktrees.values())
    # ...and each worktree is a distinct path, so the two MRs can't collide
    assert len(set(worktrees.values())) == 2


async def test_fixer_receives_every_repo_with_its_worktree_and_pr(tmp_path: Path) -> None:
    ctx = _multi_ctx(tmp_path)
    fixer_mock = AsyncMock(return_value=AgentResult(text=""))
    with _patch_workflow(
        triage=[_triage(target_repos=["webshop", "segment-api"])],
        fixer_mock=fixer_mock,
    ):
        await SentryFixWorkflow().run(ctx)

    repos = fixer_mock.call_args.args[0].repos
    assert [r["name"] for r in repos] == ["webshop", "segment-api"]
    assert len({r["path"] for r in repos}) == 2
    assert all(r["pr_url"] for r in repos)
    assert all(r["ci_bypass_path"].startswith(r["path"]) for r in repos)


async def test_untouched_target_repo_has_its_draft_closed(tmp_path: Path) -> None:
    """Drafts open before the fix exists, so a repo triage over-listed must
    not be left with an empty MR sitting on it."""
    ctx = _multi_ctx(tmp_path)

    def verify(branch: str, placeholder_sha: str, *, ctx: RunContext) -> str | None:
        return "nothing pushed" if ctx.cwd.name == "segment-api" else None

    with (
        _patch_workflow(
            triage=[_triage(target_repos=["webshop", "segment-api"])],
            verify_fix_pushed_side_effect=verify,
        ),
        patch("src.workflows.sentry_fix.runner.close_pr") as close_mock,
    ):
        result = await SentryFixWorkflow().run(ctx)

    assert result.status == "success"
    assert len(result.data["pr_urls"]) == 1
    assert close_mock.call_count == 1


# ── synthesis ───────────────────────────────────────────────────────────


async def test_synthesize_path_runs_each_group_in_isolation(ctx: RunContext) -> None:
    ctx.issues[:] = ["https://sentry.io/issues/1", "https://sentry.io/issues/2"]
    groups = [
        SynthesisGroup(issue_ids=["1"], root_cause="rc1"),
        SynthesisGroup(issue_ids=["2"], root_cause="rc2"),
    ]
    seen_cwds: list[Path] = []

    async def fixer_side_effect(_inp: Any, run_ctx: RunContext, **_kw: Any) -> AgentResult:
        seen_cwds.append(run_ctx.cwd)
        return AgentResult(text="")

    with _patch_workflow(
        triage=[_triage(root_cause="rc1"), _triage(root_cause="rc2")],
        synthesis=_synthesis(groups),
        fetch=[_event("1"), _event("2")],
        fixer_mock=AsyncMock(side_effect=fixer_side_effect),
    ):
        result = await SentryFixWorkflow().run(ctx)

    assert result.status == "success"
    assert len(result.data["pr_urls"]) == 2
    assert len(seen_cwds) == 2
    assert seen_cwds[0] != seen_cwds[1]


async def test_synthesis_group_repos_come_from_triage_not_the_agent(ctx: RunContext) -> None:
    """The agent only picks the partition. A repo name it invents must never
    reach the runner, which opens PRs against whatever it resolves to."""
    ctx.issues[:] = ["https://sentry.io/issues/1", "https://sentry.io/issues/2"]
    groups = [
        SynthesisGroup(
            issue_ids=["1", "2"],
            root_cause="one shared cause",
            target_repos=["hallucinated-repo"],
        )
    ]
    with _patch_workflow(
        triage=[_triage(root_cause="rc1"), _triage(root_cause="rc2")],
        synthesis=_synthesis(groups),
        fetch=[_event("1"), _event("2")],
    ):
        result = await SentryFixWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["results"][0]["group"]["target_repos"] == ["repo"]
    assert result.data["results"][0]["group"]["root_cause"] == "one shared cause"


async def test_issue_dropped_by_synthesis_still_gets_its_own_group(ctx: RunContext) -> None:
    ctx.issues[:] = ["https://sentry.io/issues/1", "https://sentry.io/issues/2"]
    with _patch_workflow(
        triage=[_triage(root_cause="rc1"), _triage(root_cause="rc2")],
        synthesis=_synthesis([SynthesisGroup(issue_ids=["1"], root_cause="rc1")]),
        fetch=[_event("1"), _event("2")],
    ):
        result = await SentryFixWorkflow().run(ctx)

    grouped = [g for r in result.data["results"] for g in r["group"]["issue_ids"]]
    assert sorted(grouped) == ["1", "2"]


async def test_one_failing_group_does_not_block_others(ctx: RunContext) -> None:
    ctx.issues[:] = ["https://sentry.io/issues/1", "https://sentry.io/issues/2"]
    groups = [
        SynthesisGroup(issue_ids=["1"], root_cause="ok"),
        SynthesisGroup(issue_ids=["2"], root_cause="boom"),
    ]

    def open_pr(branch: str, title: str, body: str, platform: str, *, ctx: RunContext) -> PRRef:
        if "boom" in body or "boom" in title:
            raise RuntimeError("upstream PR creation blew up")
        return PRRef(
            url=f"https://github.com/org/repo/pull/{branch[-1]}",
            branch=branch,
            platform="github",
        )

    with _patch_workflow(
        triage=[_triage(root_cause="ok"), _triage(root_cause="boom")],
        synthesis=_synthesis(groups),
        fetch=[_event("1"), _event("2")],
        open_pr_side_effect=open_pr,
    ):
        result = await SentryFixWorkflow().run(ctx)

    assert result.status == "success"  # at least one PR opened
    assert len(result.data["pr_urls"]) == 1
    error_results = [r for r in result.data["results"] if not r["ok"]]
    assert len(error_results) == 1
    assert "blew up" in error_results[0]["error"]


async def test_fix_path_labels_and_succeeds_when_ci_fails(ctx: RunContext) -> None:
    # A real, pushed fix always gets its PR labeled and the run reports
    # success — the CI gate hook already gave the fixer its retry budget to
    # fix anything it broke; a failure that survives that isn't a run
    # failure, just something a human can see via the recorded "ci" field.
    with (
        _patch_workflow(
            triage=[_triage()],
            ci_result=CiWatchResult(
                outcome="failure", reason="required check(s) failed: test", summary_text="red"
            ),
        ),
        patch("src.workflows.sentry_fix.runner.attach_label") as attach_label_mock,
    ):
        result = await SentryFixWorkflow().run(ctx)
    assert result.status == "success"
    assert len(result.data["pr_urls"]) == 1
    assert result.data["results"][0]["repos"]["repo"]["ci"] == "failure"
    assert attach_label_mock.call_count == 2
