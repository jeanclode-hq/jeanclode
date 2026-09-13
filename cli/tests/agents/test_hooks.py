"""Agent hooks — the Stop push gate, the CI-pass gate, and the GitLab
threaded-reply gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.activities.ci_watch.schemas import CiWatchResult
from src.activities.git.schemas import PRRef
from src.activities.respond.schemas import MentionContext
from src.agents.hooks import (
    _MAX_BLOCKS,
    bypass_marker_path,
    require_ci_pass_hook,
    require_pushed_and_ci_pass_hook,
    require_pushed_fix_hook,
    require_threaded_gitlab_reply_hook,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import ActivityEnd, ActivityStart, Event, Panel


def _stop_input() -> dict[str, Any]:
    return {
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }


def _so_input(tool_name: str = "StructuredOutput") -> dict[str, Any]:
    """A PreToolUse payload for the fixer's finalizing StructuredOutput call."""
    return {
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"changes_summary": "done"},
        "tool_use_id": "tu_1",
    }


def _decision(result: dict[str, Any]) -> str:
    return result.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _deny_reason(result: dict[str, Any]) -> str:
    return result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def test_bypass_marker_path_is_a_sibling_of_the_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "backend-repo"
    assert bypass_marker_path(worktree) == tmp_path / "backend-repo.ci-bypass"


@patch("src.agents.hooks.fix_missing_reason")
async def test_blocks_when_fix_missing(fix_missing_reason: Any, tmp_path: Path) -> None:
    fix_missing_reason.return_value = "you forgot to push"
    matcher = require_pushed_fix_hook(tmp_path, "fix/x", "placeholder-sha")
    result = await matcher.hooks[0](_stop_input(), None, {"signal": None})
    assert result == {"decision": "block", "reason": "you forgot to push"}


@patch("src.agents.hooks.fix_missing_reason")
async def test_allows_stop_when_fix_pushed(fix_missing_reason: Any, tmp_path: Path) -> None:
    fix_missing_reason.return_value = None
    matcher = require_pushed_fix_hook(tmp_path, "fix/x", "placeholder-sha")
    result = await matcher.hooks[0](_stop_input(), None, {"signal": None})
    assert result == {}


@patch("src.agents.hooks.fix_missing_reason")
async def test_allows_stop_when_verification_fails(fix_missing_reason: Any, tmp_path: Path) -> None:
    fix_missing_reason.side_effect = RuntimeError("git fetch failed")
    matcher = require_pushed_fix_hook(tmp_path, "fix/x", "placeholder-sha")
    result = await matcher.hooks[0](_stop_input(), None, {"signal": None})
    assert result == {}


@patch("src.agents.hooks.fix_missing_reason")
async def test_stops_blocking_after_max_blocks(fix_missing_reason: Any, tmp_path: Path) -> None:
    fix_missing_reason.return_value = "still missing"
    matcher = require_pushed_fix_hook(tmp_path, "fix/x", "placeholder-sha")
    hook = matcher.hooks[0]
    rounds = _MAX_BLOCKS + 2
    results = [await hook(_stop_input(), None, {"signal": None}) for _ in range(rounds)]
    blocked = [r for r in results if r.get("decision") == "block"]
    allowed = [r for r in results if r == {}]
    assert len(blocked) == _MAX_BLOCKS
    assert len(allowed) == 2


# ---------------------------------------------------------------------------
# require_ci_pass_hook — PreToolUse(StructuredOutput) hook enforcing the
# repo's own CI. Gates the schema-bound fixer's finalizing call rather than
# Stop (a Stop block lands too late — see hooks.py module docstring), and
# unlike a tool call it can't be skipped by the agent.
# ---------------------------------------------------------------------------


def _pr() -> PRRef:
    return PRRef(url="https://github.com/org/repo/pull/9", branch="fix/x", platform="github")


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason")
async def test_ci_hook_defers_when_fix_not_pushed_yet(
    fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    fix_missing_reason.return_value = "you forgot to push"
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}
    check.assert_not_called()


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_allows_stop_when_ci_finishes(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_blocks_with_summary_text_on_failure(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert _decision(result) == "deny"
    assert _deny_reason(result) == "ci is red"


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_stops_blocking_after_max_blocks(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    hook = matcher.hooks[0]
    results = [await hook(_so_input(), None, {"signal": None}) for _ in range(_MAX_BLOCKS + 2)]
    blocked = [r for r in results if _decision(r) == "deny"]
    allowed = [r for r in results if r == {}]
    assert len(blocked) == _MAX_BLOCKS
    assert len(allowed) == 2


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_bypass_marker_allows_stop_without_checking_ci(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    """The agent can drop a bypass marker to say a CI failure isn't its
    fault and get released immediately — no need to burn the retry budget
    on a failure it already knows won't respond to more fixing."""
    bypass_marker_path(tmp_path).touch()
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}
    check.assert_not_called()


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_bypass_marker_emits_panel(
    _fix_missing_reason: Any, _check: Any, tmp_path: Path
) -> None:
    """Existence is the whole signal — no reason text is required from the
    agent, but the run's own log still records that a bypass happened."""
    bypass_marker_path(tmp_path).touch()
    events: list[Event] = []
    ctx = _ctx(tmp_path)
    ctx.events.subscribe(events.append)
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=ctx)
    await matcher.hooks[0](_so_input(), None, {"signal": None})
    panels = [e for e in events if isinstance(e, Panel) and e.title == "CI Bypass"]
    assert len(panels) == 1


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_bypass_marker_short_circuits_before_max_blocks(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    """The marker can appear after some blocks have already happened — it
    still exits the loop immediately rather than waiting for the budget."""
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    hook = matcher.hooks[0]
    first = await hook(_so_input(), None, {"signal": None})
    assert _decision(first) == "deny"
    assert _deny_reason(first) == "ci is red"

    bypass_marker_path(tmp_path).touch()
    second = await hook(_so_input(), None, {"signal": None})
    assert second == {}
    assert check.call_count == 1


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_allows_stop_when_check_ci_raises(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.side_effect = RuntimeError("git rev-parse failed")
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_emits_activity_events_when_it_runs(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    """A run of check_ci inside the hook must be visible on the event bus —
    otherwise "the hook ran and let it through" and "the hook never fired"
    are indistinguishable in the step log."""
    check.return_value = CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
    events: list[Event] = []
    ctx = _ctx(tmp_path)
    ctx.events.subscribe(events.append)
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=ctx)
    await matcher.hooks[0](_so_input(), None, {"signal": None})
    ci_watch_events = [e for e in events if getattr(e, "name", None) == "ci-watch"]
    assert [type(e) for e in ci_watch_events] == [ActivityStart, ActivityEnd]
    assert ci_watch_events[1].ok is True


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_emits_failed_activity_when_check_ci_raises(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.side_effect = RuntimeError("git rev-parse failed")
    events: list[Event] = []
    ctx = _ctx(tmp_path)
    ctx.events.subscribe(events.append)
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=ctx)
    await matcher.hooks[0](_so_input(), None, {"signal": None})
    ci_watch_events = [e for e in events if getattr(e, "name", None) == "ci-watch"]
    assert [type(e) for e in ci_watch_events] == [ActivityStart, ActivityEnd]
    assert ci_watch_events[1].ok is False


def test_ci_hook_matches_structured_output_only(tmp_path: Path) -> None:
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    assert matcher.matcher == "StructuredOutput"


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ci_hook_ignores_other_tool_calls(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    """The matcher scopes it to StructuredOutput, but the callback also
    guards — an unrelated Bash call must not trigger a CI check."""
    matcher = require_ci_pass_hook(tmp_path, "fix/x", "sha0", _pr(), ctx=_ctx(tmp_path))
    result = await matcher.hooks[0](_so_input(tool_name="Bash"), None, {"signal": None})
    assert result == {}
    check.assert_not_called()


# ---------------------------------------------------------------------------
# require_pushed_and_ci_pass_hook — same CI gate as require_ci_pass_hook
# (also PreToolUse on StructuredOutput), but for issue-resolve's lazy PR
# creation: no PR exists yet, so it opens one itself (via callback) the
# first time it sees a real pushed commit.
# ---------------------------------------------------------------------------


def test_lazy_ci_hook_matches_structured_output_only(tmp_path: Path) -> None:
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", MagicMock(return_value=_pr()), ctx=_ctx(tmp_path)
    )
    assert matcher.matcher == "StructuredOutput"


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason")
async def test_lazy_ci_hook_defers_when_fix_not_pushed_yet(
    fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    fix_missing_reason.return_value = "you forgot to push"
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}
    open_pr.assert_not_called()
    check.assert_not_called()


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_opens_pr_and_allows_stop_when_ci_finishes(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}
    open_pr.assert_called_once()


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_blocks_with_summary_text_on_failure(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert _decision(result) == "deny"
    assert _deny_reason(result) == "ci is red"


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_opens_pr_only_once_across_calls(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    hook = matcher.hooks[0]
    await hook(_so_input(), None, {"signal": None})
    await hook(_so_input(), None, {"signal": None})
    assert open_pr.call_count == 1


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_stops_blocking_after_max_blocks(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="failure", reason="red", summary_text="ci is red")
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    hook = matcher.hooks[0]
    results = [await hook(_so_input(), None, {"signal": None}) for _ in range(_MAX_BLOCKS + 2)]
    blocked = [r for r in results if _decision(r) == "deny"]
    allowed = [r for r in results if r == {}]
    assert len(blocked) == _MAX_BLOCKS
    assert len(allowed) == 2


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_bypass_marker_allows_stop_without_checking_ci(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    bypass_marker_path(tmp_path).touch()
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}
    check.assert_not_called()
    open_pr.assert_called_once()


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_allows_stop_when_check_ci_raises(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.side_effect = RuntimeError("git rev-parse failed")
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr, ctx=_ctx(tmp_path)
    )
    result = await matcher.hooks[0](_so_input(), None, {"signal": None})
    assert result == {}


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_emits_activity_events_when_it_runs(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    check.return_value = CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
    events: list[Event] = []
    ctx = _ctx(tmp_path)
    ctx.events.subscribe(events.append)
    open_pr = MagicMock(return_value=_pr())
    matcher = require_pushed_and_ci_pass_hook(tmp_path, "fix/x", "sha0", open_pr, ctx=ctx)
    await matcher.hooks[0](_so_input(), None, {"signal": None})
    ci_watch_events = [e for e in events if getattr(e, "name", None) == "ci-watch"]
    assert [type(e) for e in ci_watch_events] == [ActivityStart, ActivityEnd]
    assert ci_watch_events[1].ok is True


@patch("src.agents.hooks.check_ci")
@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_lazy_ci_hook_does_not_double_announce_pr_opened(
    _fix_missing_reason: Any, check: Any, tmp_path: Path
) -> None:
    """issue_resolve's real open_pr callback (make_open_pr._open in
    workflows/issue_resolve/runner.py) already emits its own "PR Opened"
    panel right after calling the `open_pr` activity — that's the only
    place it can safely live, since the same callback is also reached
    directly (bypassing this hook) by the runner's post-invoke recheck.
    If this hook *also* emitted one, every issue_resolve run would render
    the panel twice for a single PR (caught live against a real PR during
    Stage L QA — gh api showed one open_pr call but two identical
    "PR Opened" STEP lines in the structured log)."""
    check.return_value = CiWatchResult(outcome="finish", reason="ok", summary_text="ok")
    events: list[Event] = []
    ctx = _ctx(tmp_path)
    ctx.events.subscribe(events.append)

    def open_pr_and_announce() -> PRRef:
        pr = _pr()
        ctx.emit(Panel(title="PR Opened", content=pr.url, style="green"))
        return pr

    matcher = require_pushed_and_ci_pass_hook(
        tmp_path, "fix/x", "sha0", open_pr_and_announce, ctx=ctx
    )
    await matcher.hooks[0](_so_input(), None, {"signal": None})
    pr_panels = [e for e in events if getattr(e, "title", None) == "PR Opened"]
    assert len(pr_panels) == 1
    pr_panels = [e for e in events if getattr(e, "title", None) == "PR Opened"]
    assert len(pr_panels) == 1


# ---------------------------------------------------------------------------
# require_threaded_gitlab_reply_hook — PreToolUse hook keeping GitLab replies
# inside the discussion the mention came from.
# ---------------------------------------------------------------------------


def _mention(**overrides: Any) -> MentionContext:
    base: dict[str, Any] = {
        "platform": "gitlab",
        "repo": "jdoe/webshop",
        "issue": "10",
        "surface": "issue",
        "thread_id": "6a9c1750b37d",
    }
    base.update(overrides)
    return MentionContext(**base)


def _bash_input(command: str, tool_name: str = "Bash") -> dict[str, Any]:
    return {
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_use_id": "tu_1",
    }


async def test_blocks_flat_issue_note() -> None:
    """The exact command that shipped the bug to production twice."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    result = await matcher.hooks[0](
        _bash_input('glab issue note 10 -R jdoe/webshop -m "All good!"'), None, {"signal": None}
    )
    assert _decision(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "projects/jdoe%2Fwebshop/issues/10/discussions/6a9c1750b37d/notes" in reason


async def test_blocks_flat_mr_note_without_explicit_iid() -> None:
    matcher = require_threaded_gitlab_reply_hook(_mention(issue="", pr="7", surface="pr_top_level"))
    result = await matcher.hooks[0](_bash_input('glab mr note -m "hi"'), None, {"signal": None})
    assert _decision(result) == "deny"
    assert (
        "merge_requests/7/discussions" in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


async def test_blocks_flat_note_api_post() -> None:
    matcher = require_threaded_gitlab_reply_hook(_mention())
    cmd = 'glab api --method POST "projects/jdoe%2Fwebshop/issues/10/notes" -f body="hi"'
    result = await matcher.hooks[0](_bash_input(cmd), None, {"signal": None})
    assert _decision(result) == "deny"


async def test_allows_discussion_scoped_reply() -> None:
    matcher = require_threaded_gitlab_reply_hook(_mention())
    cmd = (
        "glab api --method POST "
        '"projects/jdoe%2Fwebshop/issues/10/discussions/6a9c1750b37d/notes" -f body="hi"'
    )
    result = await matcher.hooks[0](_bash_input(cmd), None, {"signal": None})
    assert result == {}


async def test_allows_reading_notes() -> None:
    """A GET of the notes list is not a post — must not be blocked."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    result = await matcher.hooks[0](
        _bash_input("glab api projects/jdoe%2Fwebshop/issues/10/notes"), None, {"signal": None}
    )
    assert result == {}


async def test_allows_unrelated_commands() -> None:
    matcher = require_threaded_gitlab_reply_hook(_mention())
    for cmd in (
        "glab issue view 10 -R jdoe/webshop --comments",
        'glab issue update 10 -R jdoe/webshop --label "jeanclode:resolve"',
        "git push origin HEAD",
    ):
        assert await matcher.hooks[0](_bash_input(cmd), None, {"signal": None}) == {}


async def test_numeric_reply_body_is_not_mistaken_for_the_iid() -> None:
    """`-m "5"` is a body, not an issue number — the iid is still 10."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    result = await matcher.hooks[0](
        _bash_input('glab issue note -R jdoe/webshop -m "5" 10'), None, {"signal": None}
    )
    assert _decision(result) == "deny"


async def test_allows_flat_note_on_a_different_issue() -> None:
    """A follow-up issue is a different thread — leave it alone."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    result = await matcher.hooks[0](
        _bash_input('glab issue note 42 -R jdoe/webshop -m "Tracked here."'),
        None,
        {"signal": None},
    )
    assert result == {}


async def test_blocks_flat_note_hidden_in_a_command_chain() -> None:
    """A separator inside a quoted body must not hide the flat note."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    cmd = 'glab issue update 10 -R jdoe/webshop --label x && glab issue note 10 -m "a | b"'
    result = await matcher.hooks[0](_bash_input(cmd), None, {"signal": None})
    assert _decision(result) == "deny"


async def test_ignores_non_bash_tools() -> None:
    matcher = require_threaded_gitlab_reply_hook(_mention())
    payload = _bash_input("glab issue note 10", tool_name="Read")
    assert await matcher.hooks[0](payload, None, {"signal": None}) == {}


async def test_stops_blocking_flat_notes_after_max_blocks() -> None:
    """Escape valve: an unthreaded reply still beats no reply at all."""
    matcher = require_threaded_gitlab_reply_hook(_mention())
    hook = matcher.hooks[0]
    results = [
        await hook(_bash_input('glab issue note 10 -m "hi"'), None, {"signal": None})
        for _ in range(_MAX_BLOCKS + 2)
    ]
    assert len([r for r in results if _decision(r) == "deny"]) == _MAX_BLOCKS
    assert len([r for r in results if r == {}]) == 2


def test_hook_matches_bash_only() -> None:
    assert require_threaded_gitlab_reply_hook(_mention()).matcher == "Bash"


def test_no_hook_without_thread_id() -> None:
    """Nothing to steer toward — blocking would leave the mention unanswered."""
    assert require_threaded_gitlab_reply_hook(_mention(thread_id="")) is None


def test_no_hook_for_github() -> None:
    assert require_threaded_gitlab_reply_hook(_mention(platform="github")) is None


def test_no_hook_without_a_parent() -> None:
    assert require_threaded_gitlab_reply_hook(_mention(issue="", pr="")) is None
