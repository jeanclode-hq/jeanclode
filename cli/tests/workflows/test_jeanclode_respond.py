"""Integration tests for JeanclodeRespondWorkflow.

The planner executes every action itself via Bash/git — the workflow
only invokes the planner and, deterministically, checks whether the
branch's tip on origin moved during the turn (never trusting what the
planner reported) to decide whether to re-attach ``jeanclode:review``.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from src.activities.ci_watch.schemas import CiWatchResult
from src.activities.git.pr import pr_id
from src.activities.respond.schemas import MentionContext
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import AgentStart, Event, Panel
from src.workflows import WORKFLOWS, find_workflow
from src.workflows.jeanclode_respond.runner import JeanclodeRespondWorkflow
from src.workflows.jeanclode_respond.utils import (
    load_mention_context,
    parse_planner_output,
    pr_ref_from_mention,
)


def _write_context(
    cwd: Path,
    *,
    platform: str = "github",
    repo: str = "acme/app",
    pr: str = "7",
    surface: str = "pr_top_level",
    target_url: str = "https://github.com/acme/app/pull/7",
    mention_body: str = "please fix",
    pr_author: str = "",
) -> None:
    base = cwd / ".context"
    base.mkdir(parents=True, exist_ok=True)
    (base / "platform").write_text(platform + "\n")
    (base / "repo").write_text(repo + "\n")
    (base / "pr").write_text(pr + "\n")
    (base / "surface").write_text(surface + "\n")
    (base / "target_url").write_text(target_url + "\n")
    (base / "mention_body").write_text(mention_body + "\n")
    if pr_author:
        (base / "pr_author").write_text(pr_author + "\n")


def _ctx(tmp_path: Path, *, dry_run: bool = False) -> tuple[RunContext, list[Event]]:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    return (
        RunContext(cwd=tmp_path, workspace=tmp_path, events=bus, dry_run=dry_run),
        received,
    )


def _result_message(structured: dict) -> ResultMessage:
    msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result="",
    )
    object.__setattr__(msg, "structured_output", structured)
    return msg


class _ScriptedQuery:
    """Returns scripted responses by matching prompt substrings."""

    def __init__(self) -> None:
        self.responses: list[tuple[str, dict]] = []

    def add(self, match: str, structured: dict) -> None:
        self.responses.append((match, structured))

    def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        for i, (match, structured) in enumerate(self.responses):
            if match in prompt:
                self.responses.pop(i)
                return self._gen(structured)
        return self._gen({})

    async def _gen(self, structured: dict) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield _result_message(structured)


def _completed(rc: int = 0, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _git_fake_run(*, branch: str = "feat/x", tip: str | None = None):
    """Fake ``subprocess.run`` for git plumbing used by the SHA check.

    ``tip`` is the SHA ``git ls-remote`` reports for ``branch`` — pass a
    different value than the starting SHA to simulate a push during the
    planner's turn, or ``None`` to simulate an unresolvable ref.
    """
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed(0, stdout="starting-sha\n")
        if cmd[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return _completed(0, stdout=f"{branch}\n")
        if cmd[:3] == ["git", "ls-remote", "origin"]:
            if tip is None:
                return _completed(0, stdout="")
            return _completed(0, stdout=f"{tip}\trefs/heads/{branch}\n")
        return _completed(0)

    return fake_run, seen


# ── load_mention_context / parsing helpers ────────────────────────────


def test_load_mention_context_reads_files(tmp_path: Path) -> None:
    _write_context(tmp_path)
    mention = load_mention_context(tmp_path)
    assert mention is not None
    assert mention.platform == "github"
    assert mention.repo == "acme/app"
    assert mention.pr == "7"
    assert mention.surface == "pr_top_level"


def test_load_mention_context_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_mention_context(tmp_path) is None


def test_load_mention_context_returns_none_for_unknown_platform(tmp_path: Path) -> None:
    base = tmp_path / ".context"
    base.mkdir()
    (base / "platform").write_text("bitbucket\n")
    (base / "repo").write_text("acme/app\n")
    assert load_mention_context(tmp_path) is None


def test_parse_planner_output_validates_structured() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text="", structured={"actions_taken": ["handle"], "summary": "did it"})
    output = parse_planner_output(r)
    assert output is not None
    assert output.actions_taken == ["handle"]
    assert output.summary == "did it"


def test_parse_planner_output_returns_none_for_invalid() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text="", structured={"bogus": "shape"})
    assert parse_planner_output(r) is None


# ── registry ──────────────────────────────────────────────────────────


def test_workflow_is_registered() -> None:
    assert WORKFLOWS["jeanclode-respond"] is JeanclodeRespondWorkflow


def test_workflow_command_lookup() -> None:
    assert find_workflow(command="respond") is JeanclodeRespondWorkflow


# ── workflow paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_planner_self_executed_actions_return_success(tmp_path: Path) -> None:
    """The workflow trusts the planner's report (it already executed the
    action itself) and returns success regardless of which action kind."""
    _write_context(tmp_path)
    ctx, received = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add(
        "JeanClode",
        {"actions_taken": ["handle"], "summary": "Replied 'Triggering a review.'"},
    )
    fake_run, _ = _git_fake_run(tip="starting-sha")  # unchanged

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["actions_taken"] == ["handle"]
    assert result.data["relabeled"] is False
    assert "Triggering a review" in result.summary
    agent_names = [e.name for e in received if isinstance(e, AgentStart)]
    assert agent_names == ["respond-planner"]
    assert any(isinstance(e, Panel) and e.title.startswith("Planner:") for e in received)


@pytest.mark.asyncio
async def test_route_and_handle_combine_in_one_turn(tmp_path: Path) -> None:
    _write_context(tmp_path)
    ctx, _ = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add(
        "JeanClode",
        {"actions_taken": ["handle", "route"], "summary": "Replied, then routed to resolve"},
    )
    fake_run, _ = _git_fake_run(tip="starting-sha")

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["actions_taken"] == ["handle", "route"]


@pytest.mark.asyncio
async def test_relabels_when_planner_pushes_new_commits(tmp_path: Path) -> None:
    """Deterministic SHA check: origin's branch tip moved during the
    turn → relabel, regardless of what the planner self-reported.

    ``utils.subprocess`` and ``relabel.subprocess`` are both bindings to
    the same global ``subprocess`` module, so one patch target covers
    both the git plumbing and the ``gh``/``glab`` relabel calls.
    """
    _write_context(tmp_path)
    ctx, received = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("JeanClode", {"actions_taken": ["handle"], "summary": "Fixed the typo and pushed"})
    fake_run, seen = _git_fake_run(branch="feat/x", tip="new-sha")

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["relabeled"] is True
    assert any(c[:3] == ["git", "ls-remote", "origin"] for c in seen)
    assert any("--add-label" in c for c in seen)
    assert any(isinstance(e, Panel) and e.title == "Re-labelled for review" for e in received)


@pytest.mark.asyncio
async def test_no_relabel_when_branch_unchanged(tmp_path: Path) -> None:
    _write_context(tmp_path)
    ctx, _ = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("JeanClode", {"actions_taken": ["handle"], "summary": "Just replied"})
    fake_run, seen = _git_fake_run(tip="starting-sha")  # tip == starting sha

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["relabeled"] is False
    assert not any("--add-label" in c for c in seen)


@pytest.mark.asyncio
async def test_no_sha_check_for_issue_mention(tmp_path: Path) -> None:
    """Issue mentions have no ``pr`` — nothing to compare, no relabel."""
    _write_context(
        tmp_path, pr="", surface="issue", target_url="https://github.com/acme/app/issues/9"
    )
    (tmp_path / ".context" / "issue").write_text("9\n")
    ctx, _ = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("JeanClode", {"actions_taken": ["route"], "summary": "Routed to resolve"})

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.respond.relabel.subprocess.run") as relabel_run,
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["relabeled"] is False
    relabel_run.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_returns_error_when_context_missing(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    result = await JeanclodeRespondWorkflow().run(ctx)
    assert result.status == "error"
    assert ".context" in result.summary


@pytest.mark.asyncio
async def test_workflow_handles_invalid_planner_output(tmp_path: Path) -> None:
    _write_context(tmp_path)
    ctx, _ = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("JeanClode", {"not": "a PlannerOutput"})
    fake_run, _ = _git_fake_run(tip="starting-sha")

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "error"
    assert "no action" in result.summary


@pytest.mark.asyncio
async def test_dry_run_short_circuits_before_planner(tmp_path: Path) -> None:
    _write_context(tmp_path)
    ctx, _ = _ctx(tmp_path, dry_run=True)

    sq = _ScriptedQuery()

    with (
        patch("src.agents.base.query", side_effect=sq) as q,
        patch("src.workflows.jeanclode_respond.utils.subprocess.run") as git_run,
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["dry_run"] is True
    q.assert_not_called()
    git_run.assert_not_called()


# ── GitLab threaded-reply hook wiring ─────────────────────────────────


def _write_gitlab_issue_context(cwd: Path, *, thread_id: str) -> None:
    base = cwd / ".context"
    base.mkdir(parents=True, exist_ok=True)
    (base / "platform").write_text("gitlab\n")
    (base / "repo").write_text("jdoe/webshop\n")
    (base / "issue").write_text("10\n")
    (base / "surface").write_text("issue\n")
    (base / "thread_id").write_text(thread_id + "\n")
    (base / "mention_body").write_text("@jeanclode-bot how are you\n")


async def _run_capturing_options(tmp_path: Path) -> Any:
    """Run the workflow, returning the ``ClaudeAgentOptions`` the planner got."""
    ctx, _ = _ctx(tmp_path)
    captured: list[Any] = []
    sq = _ScriptedQuery()
    sq.add("JeanClode", {"actions_taken": ["handle"], "summary": "Replied."})

    def capture(*, prompt: str, options: Any) -> Any:
        captured.append(options)
        return sq(prompt=prompt, options=options)

    fake_run, _ = _git_fake_run(tip="starting-sha")
    with (
        patch("src.agents.base.query", side_effect=capture),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
    ):
        await JeanclodeRespondWorkflow().run(ctx)
    return captured[0]


@pytest.mark.asyncio
async def test_gitlab_mention_wires_threaded_reply_hook(tmp_path: Path) -> None:
    """A resolved thread id must reach the planner as an enforced PreToolUse hook.

    Prompt wording alone twice failed to stop the agent posting a flat
    note, so the guard has to be present as a hook, not just as text.
    """
    _write_gitlab_issue_context(tmp_path, thread_id="6a9c1750b37d")
    options = await _run_capturing_options(tmp_path)

    assert list(options.hooks or {}) == ["PreToolUse"]
    matcher = options.hooks["PreToolUse"][0]
    assert matcher.matcher == "Bash"

    blocked = await matcher.hooks[0](
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": 'glab issue note 10 -R jdoe/webshop -m "hi"'},
            "tool_use_id": "t",
        },
        None,
        {"signal": None},
    )
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_no_hook_when_thread_id_unresolved(tmp_path: Path) -> None:
    """Without a thread id the flat note is the only option — don't block it."""
    _write_gitlab_issue_context(tmp_path, thread_id="")
    options = await _run_capturing_options(tmp_path)
    assert not options.hooks


@pytest.mark.asyncio
async def test_no_threaded_reply_hook_for_github_mention(tmp_path: Path) -> None:
    _write_context(tmp_path)
    options = await _run_capturing_options(tmp_path)
    matchers = [m.matcher for m in options.hooks["PreToolUse"]]
    assert "Bash" not in matchers


# ── ready notice: the loop converging without a push ──────────────────


def _notify_ctx(tmp_path: Path) -> tuple[RunContext, list[Event]]:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    return (
        RunContext(
            cwd=tmp_path,
            workspace=tmp_path,
            events=bus,
            notify_users=["alice", "bob"],
        ),
        received,
    )


async def _run_respond(
    tmp_path: Path,
    ctx: RunContext,
    *,
    tip: str = "starting-sha",
    thread_counts: list[int | None],
    notified: bool = True,
) -> tuple[Any, list]:
    """Drive one respond turn with scripted thread counts."""
    sq = _ScriptedQuery()
    sq.add("JeanClode", {"actions_taken": ["handle"], "summary": "resolved the threads"})
    fake_run, _ = _git_fake_run(tip=tip)
    notice_calls: list = []

    def fake_notice(*args: Any, **kwargs: Any) -> bool:
        notice_calls.append((args, kwargs))
        return notified

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.jeanclode_respond.utils.subprocess.run", side_effect=fake_run),
        patch(
            "src.workflows.jeanclode_respond.runner.count_unresolved_threads",
            side_effect=thread_counts,
        ),
        patch(
            "src.workflows.jeanclode_respond.runner.post_ready_notice",
            side_effect=fake_notice,
        ),
    ):
        result = await JeanclodeRespondWorkflow().run(ctx)
    return result, notice_calls


@pytest.mark.asyncio
async def test_notifies_when_last_thread_resolved_without_a_push(tmp_path: Path) -> None:
    """The silent exit this feature exists to close: the planner resolved
    every finding as a false positive, pushed nothing, so no relabel fires
    and no re-review ever posts LGTM."""
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, received = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[2, 0])

    assert result.data["relabeled"] is False
    assert result.data["notified"] is True
    assert len(notice_calls) == 1
    assert notice_calls[0][0][3] == ["alice", "bob"]
    assert any(isinstance(e, Panel) and e.title == "Reviewers notified" for e in received)


@pytest.mark.asyncio
async def test_no_notice_while_threads_are_still_open(tmp_path: Path) -> None:
    """Mid-loop: something is still unresolved, so the loop has not
    converged and nobody is summoned yet."""
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, _ = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[3, 1])

    assert result.data["notified"] is False
    assert notice_calls == []


@pytest.mark.asyncio
async def test_no_notice_when_the_planner_pushed(tmp_path: Path) -> None:
    """A push relabels for another review round, which owns the notice from
    there — pinging here would summon people mid-loop."""
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, _ = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, tip="new-sha", thread_counts=[2, 0])

    assert result.data["relabeled"] is True
    assert result.data["notified"] is False
    assert notice_calls == []


@pytest.mark.asyncio
async def test_no_notice_on_a_human_authored_mr(tmp_path: Path) -> None:
    """Respond fires on anyone's mention. A human's MR where someone asked
    the bot to resolve a thread is not Jeanclode finishing its own work."""
    _write_context(tmp_path, pr_author="alice")
    ctx, _ = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[2, 0])

    assert result.data["notified"] is False
    assert notice_calls == []


@pytest.mark.asyncio
async def test_no_notice_when_nobody_is_configured(tmp_path: Path) -> None:
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, _ = _ctx(tmp_path)  # notify_users empty

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[2, 0])

    assert result.data["notified"] is False
    assert notice_calls == []


@pytest.mark.asyncio
async def test_unreadable_thread_count_does_not_notify(tmp_path: Path) -> None:
    """None is "don't know", not "all resolved" — an API blip must not read
    as a converged loop."""
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, _ = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[None, 0])

    assert result.data["notified"] is False
    assert notice_calls == []


@pytest.mark.asyncio
async def test_mr_with_no_threads_at_all_does_not_notify(tmp_path: Path) -> None:
    """Nothing was open before the turn, so this turn converged nothing —
    an ordinary mention on a bot MR must stay quiet."""
    _write_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx, _ = _notify_ctx(tmp_path)

    result, notice_calls = await _run_respond(tmp_path, ctx, thread_counts=[0, 0])

    assert result.data["notified"] is False
    assert notice_calls == []


# ── ci gate on a turn that pushed code ────────────────────────────────


def test_pr_ref_from_mention_rebuilds_the_pr_url() -> None:
    """target_url is the comment URL, so the PR/MR url has to be rebuilt —
    pr_id reads its trailing segment to address the pipeline."""
    gh = pr_ref_from_mention(
        MentionContext(
            platform="github",
            repo="acme/app",
            pr="7",
            target_url="https://github.com/acme/app/pull/7#issuecomment-99",
        ),
        "feat/x",
    )
    assert gh is not None
    assert gh.url == "https://github.com/acme/app/pull/7"
    assert pr_id(gh) == "7"

    gl = pr_ref_from_mention(
        MentionContext(
            platform="gitlab",
            repo="grp/proj",
            pr="42",
            target_url="https://gitlab.example.com/grp/proj/-/merge_requests/42#note_5",
        ),
        "feat/x",
    )
    assert gl is not None
    assert gl.url == "https://gitlab.example.com/grp/proj/-/merge_requests/42"
    assert pr_id(gl) == "42"


def test_pr_ref_from_mention_needs_a_pr_and_a_branch() -> None:
    issue = MentionContext(
        platform="github",
        repo="acme/app",
        issue="10",
        target_url="https://github.com/acme/app/issues/10#issuecomment-1",
    )
    assert pr_ref_from_mention(issue, "feat/x") is None

    pr = MentionContext(
        platform="github",
        repo="acme/app",
        pr="7",
        target_url="https://github.com/acme/app/pull/7",
    )
    assert pr_ref_from_mention(pr, "") is None


def _ci_hook(options: Any) -> Any:
    matchers = [m for m in options.hooks["PreToolUse"] if m.matcher == "StructuredOutput"]
    assert len(matchers) == 1
    return matchers[0].hooks[0]


async def _fire_ci_hook(hook: Any) -> dict:
    return await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "StructuredOutput",
            "tool_input": {},
            "tool_use_id": "t",
        },
        None,
        {"signal": None},
    )


@pytest.mark.asyncio
async def test_pr_mention_wires_the_ci_gate(tmp_path: Path) -> None:
    _write_context(tmp_path)
    options = await _run_capturing_options(tmp_path)
    assert _ci_hook(options) is not None


@pytest.mark.asyncio
async def test_issue_mention_has_no_ci_gate(tmp_path: Path) -> None:
    """No branch to verify — an issue mention must not pay for a CI check."""
    _write_gitlab_issue_context(tmp_path, thread_id="6a9c1750b37d")
    options = await _run_capturing_options(tmp_path)
    assert [m.matcher for m in options.hooks["PreToolUse"]] == ["Bash"]


@pytest.mark.asyncio
async def test_ci_gate_is_a_noop_when_the_turn_pushed_nothing(tmp_path: Path) -> None:
    _write_context(tmp_path)
    options = await _run_capturing_options(tmp_path)
    hook = _ci_hook(options)

    with (
        patch("src.agents.hooks.fix_missing_reason", return_value="nothing pushed"),
        patch("src.agents.hooks.check_ci") as check,
    ):
        assert await _fire_ci_hook(hook) == {}
    check.assert_not_called()


@pytest.mark.asyncio
async def test_ci_gate_blocks_finalizing_on_a_red_pipeline(tmp_path: Path) -> None:
    _write_context(tmp_path)
    options = await _run_capturing_options(tmp_path)
    hook = _ci_hook(options)

    red = CiWatchResult(outcome="failure", reason="1 failed", summary_text="ci-watch: failure")
    with (
        patch("src.agents.hooks.fix_missing_reason", return_value=None),
        patch("src.agents.hooks.check_ci", return_value=red),
    ):
        blocked = await _fire_ci_hook(hook)

    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ci-watch: failure" in blocked["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_ci_gate_lets_a_green_pipeline_through(tmp_path: Path) -> None:
    _write_context(tmp_path)
    options = await _run_capturing_options(tmp_path)
    hook = _ci_hook(options)

    green = CiWatchResult(outcome="finish", reason="all passed", summary_text="ok")
    with (
        patch("src.agents.hooks.fix_missing_reason", return_value=None),
        patch("src.agents.hooks.check_ci", return_value=green),
    ):
        assert await _fire_ci_hook(hook) == {}
