"""Reusable SDK hooks that enforce what a prompt alone cannot.

Every hook here exists because an agent that ignores an instruction is
otherwise indistinguishable from one that followed it: the workflow
reports success and the gap only shows up in production.

- ``require_pushed_fix_hook`` (Stop) — fixer agents commit and push
  their own fix via Bash; nothing else in the pipeline does it for
  them. Without this, an agent that gets stuck exploring and never
  edits anything still ends its turn cleanly and the runner only finds
  out once it's about to mark the PR ready.
- ``require_ci_pass_hook`` (PreToolUse on ``StructuredOutput``) — verifies
  the pushed fix against the target repo's own CI rather than exposing it
  as a tool the agent could skip (see
  ``docs/adr/008-ci-gated-fix-verification.md``). Only proceeds once
  ``require_pushed_fix_hook`` reports a real, pushed commit. Used by
  sentry-fix, which opens its PR eagerly before the fixer runs. A failure
  that survives the retry budget doesn't block the PR — see the ADR — but
  the agent can also cut the loop short itself the moment it's confident a
  failure isn't its own doing, via the bypass marker file (see
  ``bypass_marker_path``), instead of burning the rest of its retries on
  something it can't fix.

  This gates the fixer's ``StructuredOutput`` call, not ``Stop``. The
  fixer runs with ``output_schema`` set, so it finalizes its turn by
  calling ``StructuredOutput`` — and a ``Stop`` hook fires *after* that
  finalization, too late: the CLI drops the block rather than retract the
  already-submitted structured output
  (``tengu_structured_output_late_retraction_drop``), so the fix-and-recheck
  loop never actually looped. ``PreToolUse`` fires *before* the tool runs,
  so ``permissionDecision: "deny"`` feeds the CI result back as tool output
  and the agent keeps working in the same session (cache and context
  intact) until CI is green, bypassed, or the retry budget is spent.
- ``require_pushed_and_ci_pass_hook`` (PreToolUse on ``StructuredOutput``)
  — same CI gate, same bypass marker, same reason for gating
  ``StructuredOutput`` rather than ``Stop``, but for callers that don't
  have a PR yet when the fixer starts: it opens one itself, via a callback,
  the first time it sees a real pushed commit. Used by issue-resolve for
  every target repo, single- or multi-repo alike — opening a PR
  speculatively before it has real content risks an empty/wrong PR, so PR
  creation is deferred until there's something real to open it for. One
  instance guards one target repo; a multi-repo fix registers one pair
  (this + ``require_pushed_fix_hook``) per repo on the same fixer session.
- ``require_demo_hook`` (PreToolUse on ``StructuredOutput``) — once CI
  lets the fixer through, a separate demo agent records the UI change
  working; any verdict but ``ok`` goes back to the fixer. ``GateResults``
  makes it wait for the CI gates on the same call, and
  ``forbid_publishing_hook`` keeps the demo agent from committing or pushing.
- ``require_threaded_gitlab_reply_hook`` (PreToolUse) — the respond
  planner types its own ``glab`` commands, and the flat
  ``glab issue note`` / ``glab mr note`` shortcuts post a disconnected
  top-level comment rather than a reply inside the discussion the
  mention came from.

Each blocks the offending event and feeds back what's missing so the
agent can correct itself within the same turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from claude_agent_sdk import HookContext, HookMatcher, PreToolUseHookInput, StopHookInput

from src.activities.ci_watch import check_ci
from src.activities.demo import (
    BYPASS_FILE,
    DEMO_DIR,
    HINTS_FILE,
    DemoGateState,
    DemoRound,
    git_status,
    reset_demo_dir,
    restore_setup,
    stash_setup,
)
from src.activities.demo.checkout import head_sha
from src.activities.demo.processes import running_pids, stop_new_processes
from src.activities.git.ops import fix_missing_reason
from src.activities.git.schemas import PRRef, WorktreePath
from src.activities.respond.schemas import MentionContext
from src.runtime.context import RunContext
from src.runtime.events import ActivityEnd, ActivityStart, Panel

logger = logging.getLogger(__name__)

# Caps how many times these hooks will block a single agent turn on a
# still-red CI check. Once exhausted, the hook stops blocking and the
# fixer's turn ends — CI going green is no longer required at that point:
# the runner labels the PR for review either way (see issue_resolve/runner.py
# and sentry_fix/runner.py). This budget only bounds how many fix-and-recheck
# rounds the agent gets, not whether the work ultimately lands.
#
# Every block is a full push-and-wait-for-CI round trip, so this trades the
# fixer's wall-clock against how often it gives up on a fix that was nearly
# right. A red MR costs a human more than a slow one does, hence the headroom.
_MAX_BLOCKS = 6

# CI gates get more rounds than the other hooks: past this, a human can still
# ask the respond workflow to keep going on the PR.
_MAX_CI_BLOCKS = 10

# The SDK-internal tool the agent calls to emit its `output_schema` result.
# It's how a schema-bound agent finalizes its turn, so gating it is
# equivalent to gating "the agent is about to declare itself done" — but
# early enough that a `deny` still lands (see module docstring).
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

_CI_HOOK_TIMEOUT = 1500

_BYPASS_PANEL_TEXT = (
    "Fixer marked this repo's CI failure as unrelated to its change — no further checks."
)


def bypass_marker_path(cwd: Path) -> Path:
    """Where the fixer can signal "this CI failure isn't mine, stop asking."

    One directory above the worktree, named after it — e.g. a worktree at
    ``.../worktrees/fix-123/backend-repo`` gets
    ``.../worktrees/fix-123/backend-repo.ci-bypass``. Outside the git
    working tree on purpose: a plain file living inside it could get swept
    up by a broad ``git add``, which would silently turn "not my problem"
    into a committed file. The path is deterministic from ``cwd`` alone, so
    both the hook and the prompt (via ``IssueFixerInput``/``FixerInput``)
    compute the exact same location independently — nothing new to thread
    through the hook's own signature.

    Existence is the whole signal — the agent just touches the file, no
    reason text required. Nobody downstream of jeanclode reads this file or
    anything derived from its content, so asking the agent to compose a
    reason would be pure overhead; the run's own event log already says
    which repo bypassed and why the hook let it (see the "CI Bypass" panel
    each hook emits).
    """
    return cwd.parent / f"{cwd.name}.ci-bypass"


def _bypassed(cwd: Path, *, ctx: RunContext) -> bool:
    """True and logged, or False — shared by both CI gate hooks below."""
    if not bypass_marker_path(cwd).exists():
        return False
    ctx.emit(Panel(title="CI Bypass", content=_BYPASS_PANEL_TEXT, style="yellow"))
    return True


def _deny_structured_output(reason: str) -> dict[str, Any]:
    """A PreToolUse `deny` on `StructuredOutput` — the fixer gets `reason`
    back as tool output and continues the same turn instead of finalizing.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def require_pushed_fix_hook(cwd: Path, branch: str, placeholder_sha: str) -> HookMatcher:
    """Stop hook: block the agent from finishing until its fix is committed and pushed."""
    blocks = 0

    async def _hook(
        _input: StopHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        nonlocal blocks
        if blocks >= _MAX_BLOCKS:
            return {}
        try:
            reason = fix_missing_reason(cwd, branch, placeholder_sha)
        except RuntimeError:
            # Couldn't verify (e.g. a transient git-fetch failure) — don't
            # block on unreliable information. The runner's own
            # verify_fix_pushed call is the authoritative gate before the
            # PR is marked ready.
            return {}
        if reason is None:
            return {}
        blocks += 1
        return {"decision": "block", "reason": reason}

    return HookMatcher(hooks=[_hook])


def require_ci_pass_hook(
    cwd: Path, branch: str, placeholder_sha: str, pr: PRRef, *, ctx: RunContext
) -> HookMatcher:
    """PreToolUse(`StructuredOutput`) hook: keep the fixer from finalizing
    its turn until the repo's own CI passes on its pushed commit, there's
    nothing to verify against, the retry budget runs out, or the agent
    creates `bypass_marker_path(cwd)` to say a still-red result isn't its
    fault.

    Gates `StructuredOutput` rather than `Stop` because the fixer is
    schema-bound: it ends its turn *by* calling `StructuredOutput`, and a
    `Stop` block lands after that and gets dropped (see module docstring).

    `check_ci` runs via `asyncio.to_thread` — it's blocking and can take
    minutes, and this hook shares an event loop with other concurrent
    groups (`asyncio.gather` in the workflow).
    """
    blocks = 0

    async def _hook(
        hook_input: PreToolUseHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        nonlocal blocks
        if hook_input.get("tool_name") != _STRUCTURED_OUTPUT_TOOL:
            return {}
        if blocks >= _MAX_CI_BLOCKS:
            return {}
        try:
            missing = fix_missing_reason(cwd, branch, placeholder_sha)
        except RuntimeError:
            return {}
        if missing is not None:
            # require_pushed_fix_hook already blocks the Stop on this.
            return {}

        if _bypassed(cwd, ctx=ctx):
            return {}

        # check_ci isn't @activity (it's called off-context, from poll.py, by
        # both this hook and the runner) — emit manually so a hook run is
        # visible in the step log instead of indistinguishable from one that
        # never fired.
        ctx.emit(ActivityStart(name="ci-watch"))
        started = time.monotonic()
        result = None
        try:
            result = await asyncio.to_thread(check_ci, pr, cwd=cwd)
        except Exception:
            # Don't block on unreliable info — see require_pushed_fix_hook.
            logger.warning("require_ci_pass_hook: check_ci failed", exc_info=True)
        finally:
            ctx.emit(
                ActivityEnd(
                    name="ci-watch",
                    ok=result is not None and result.outcome == "finish",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        if result is None or result.outcome == "finish":
            return {}
        blocks += 1
        return _deny_structured_output(result.summary_text)

    # check_ci's own wait loop can run up to ~1215s (settle + wait-timeout,
    # see poll.py) — the SDK's HookMatcher default timeout is only 60s,
    # which would kill this hook's callback long before check_ci returns.
    return HookMatcher(matcher=_STRUCTURED_OUTPUT_TOOL, hooks=[_hook], timeout=_CI_HOOK_TIMEOUT)


def require_pushed_and_ci_pass_hook(
    cwd: Path,
    branch: str,
    placeholder_sha: str,
    open_pr: Callable[[], PRRef],
    *,
    ctx: RunContext,
    requester_asks: str = "",
) -> HookMatcher:
    """PreToolUse(``StructuredOutput``) hook: keep the agent from finalizing
    until this repo's fix is pushed and its own CI passes — same gate as
    ``require_ci_pass_hook`` (and gates ``StructuredOutput`` for the same
    reason), except there's no PR yet when the fixer starts. ``open_pr`` is
    called at most once, the first time a real pushed commit is seen, and
    its result is reused for every check_ci call after (including from the
    runner's own post-invoke authoritative recheck, which passes the same
    memoized callback).

    A repo the fixer never touches (e.g. one of several target repos
    triage listed that turned out not to need a change) never calls
    ``open_pr`` at all — no PR, no CI check, no error. That's the point:
    speculative PRs per target repo are exactly what this avoids.

    With ``requester_asks``, the call that opens the PR is denied once with
    its URL: the asks often target the PR itself (assignee, reviewers), which
    the fixer can't act on before it exists.
    """
    blocks = 0
    pr: PRRef | None = None

    async def _hook(
        hook_input: PreToolUseHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        nonlocal blocks, pr
        if hook_input.get("tool_name") != _STRUCTURED_OUTPUT_TOOL:
            return {}
        if blocks >= _MAX_CI_BLOCKS:
            return {}
        try:
            missing = fix_missing_reason(cwd, branch, placeholder_sha)
        except RuntimeError:
            return {}
        if missing is not None:
            # require_pushed_fix_hook already blocks the Stop on this.
            return {}

        if pr is None:
            # open_pr (make_open_pr._open in issue_resolve/runner.py) emits
            # its own "PR Opened" panel — don't duplicate it here. It's the
            # only safe owner of that emit since the runner's post-invoke
            # recheck can also call it directly, bypassing this hook.
            pr = open_pr()
            if requester_asks:
                return _deny_structured_output(
                    f"Your PR/MR is open: {pr.url}. Carry out the requester's asks that "
                    "concern it, then finish again:\n\n" + requester_asks
                )

        if _bypassed(cwd, ctx=ctx):
            return {}

        ctx.emit(ActivityStart(name="ci-watch"))
        started = time.monotonic()
        result = None
        try:
            result = await asyncio.to_thread(check_ci, pr, cwd=cwd)
        except Exception:
            logger.warning("require_pushed_and_ci_pass_hook: check_ci failed", exc_info=True)
        finally:
            ctx.emit(
                ActivityEnd(
                    name="ci-watch",
                    ok=result is not None and result.outcome == "finish",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        if result is None or result.outcome == "finish":
            return {}
        blocks += 1
        return _deny_structured_output(result.summary_text)

    return HookMatcher(matcher=_STRUCTURED_OUTPUT_TOOL, hooks=[_hook], timeout=_CI_HOOK_TIMEOUT)


# --------------------------------------------------------------------------
# Demo gate
# --------------------------------------------------------------------------

MAX_DEMO_ROUNDS = _MAX_BLOCKS

# One demo session's budget: installing a frontend from scratch, starting it and
# recording. Past it the round counts as failed and the fixer decides what next.
DEMO_AGENT_TIMEOUT_S = 2400

# Waits out the CI gates on the same call first, then runs one demo session.
_DEMO_HOOK_TIMEOUT = _CI_HOOK_TIMEOUT + DEMO_AGENT_TIMEOUT_S + 300

_DEMO_NEXT_STEP = {
    "broken": (
        "Fix the code so the change does what the issue asks, push, and finish again: "
        "a new round runs on your new commit."
    ),
    "unavailable": (
        "The demo agent couldn't get the app running. If you know what it's missing "
        "(env vars, how to log in, the start command, which page shows the change), "
        "write it to {hints} and finish again."
    ),
    "nothing_to_show": (
        "The demo agent found nothing visible to show. If the change is visible "
        "somewhere it didn't look (a page, a state, a click path), write that to "
        "{hints} and finish again."
    ),
}


def _is_deny(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    specific = result.get("hookSpecificOutput") or {}
    return specific.get("permissionDecision") == "deny" or result.get("decision") == "block"


class GateResults:
    """What the CI gates answered on each ``StructuredOutput`` call.

    The CLI runs every hook matching a tool call in parallel, so the demo gate
    can't just sit after the CI gates in the list: it waits here until each one
    has answered the same call, and stands down if any of them denied it.
    """

    def __init__(self) -> None:
        self._expected = 0
        self._denied: dict[str, list[bool]] = {}
        self._changed = asyncio.Condition()

    def track(self, matcher: HookMatcher) -> HookMatcher:
        """Return ``matcher`` with its callbacks reporting here."""
        self._expected += len(matcher.hooks)
        return HookMatcher(
            matcher=matcher.matcher,
            hooks=[self._wrap(h) for h in matcher.hooks],
            timeout=matcher.timeout,
        )

    def _wrap(self, hook: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def _tracked(
            hook_input: PreToolUseHookInput, tool_use_id: str | None, context: HookContext
        ) -> dict[str, Any]:
            result: dict[str, Any] = {}
            try:
                result = await hook(hook_input, tool_use_id, context)
                return result
            finally:
                await self._record(_call_id(hook_input, tool_use_id), _is_deny(result))

        return _tracked

    async def _record(self, call: str, denied: bool) -> None:
        async with self._changed:
            self._denied.setdefault(call, []).append(denied)
            self._changed.notify_all()

    async def any_denied(self, call: str) -> bool:
        async with self._changed:
            await self._changed.wait_for(lambda: len(self._denied.get(call, [])) >= self._expected)
            return any(self._denied.pop(call, []))


def _call_id(hook_input: PreToolUseHookInput, tool_use_id: str | None) -> str:
    return tool_use_id or str(hook_input.get("tool_use_id") or "")


def demo_bypass_reason(demo_dir: Path) -> str:
    path = demo_dir / BYPASS_FILE
    if not path.exists():
        return ""
    return path.read_text(errors="replace").strip() or "no reason given"


def _pushed(wt: WorktreePath) -> bool:
    try:
        return fix_missing_reason(wt.path, wt.branch, wt.placeholder_sha) is None
    except RuntimeError:
        return False


def _demo_feedback(demo: DemoRound, round_no: int, demo_dir: Path) -> str:
    hints = demo_dir / HINTS_FILE
    lines = [
        f"Demo round {round_no} of {MAX_DEMO_ROUNDS}: the demo agent's verdict is "
        f"`{demo.verdict}`, so you can't finish yet.",
        "",
        demo.evidence.strip() or "(no explanation given)",
    ]
    if demo.screenshots:
        lines += ["", "Screenshots (open them with Read and judge for yourself):"]
        lines += [f"- {shot}" for shot in demo.screenshots]
    lines += [
        "",
        _DEMO_NEXT_STEP[demo.verdict].format(hints=hints),
        "",
        "Never change product code just to make the demo run: no mock modes, auth "
        "bypass flags or fake-data switches in your commits. Setting up the app is "
        "the demo agent's job. If a demo genuinely can't work here, write the reason "
        f"to {demo_dir / BYPASS_FILE} and finish again.",
    ]
    return "\n".join(lines)


def _dirty_feedback(dirty: dict[str, str], demo_dir: Path) -> str:
    listing = "\n".join(f"{path}:\n{status.rstrip()}" for path, status in dirty.items())
    return (
        "The demo can't run on a checkout with uncommitted changes: everything left "
        "over after a demo round is cleared away as the demo's own setup, and yours "
        f"would go with it. `git status --porcelain` shows:\n\n{listing}\n\n"
        "Commit and push what belongs in the fix, delete the rest (build output, "
        "scratch files), then finish again. If a demo genuinely can't work here, write "
        f"the reason to {demo_dir / BYPASS_FILE} and finish again."
    )


def _crash_feedback(error: str, round_no: int, demo_dir: Path) -> str:
    left = MAX_DEMO_ROUNDS - round_no
    return (
        f"Demo round {round_no} of {MAX_DEMO_ROUNDS}: the demo agent failed before giving "
        f"a verdict.\n\n{error}\n\n"
        f"Each round gets {DEMO_AGENT_TIMEOUT_S // 60} minutes to install, start and record "
        f"the app; {left} round(s) left. If this looks transient (a timeout on a slow "
        "install, a rate limit, a network blip), finish again to retry, and write anything "
        f"that would speed the next round up (the start command, what to skip) to "
        f"{demo_dir / HINTS_FILE}. If it will keep failing here, write the reason to "
        f"{demo_dir / BYPASS_FILE} and finish again."
    )


def require_demo_hook(
    worktrees: list[WorktreePath],
    run_demo: Callable[[int], Awaitable[DemoRound]],
    state: DemoGateState,
    *,
    ctx: RunContext,
    ci_gates: GateResults | None = None,
    demo_dir: Path = DEMO_DIR,
) -> HookMatcher:
    """PreToolUse(``StructuredOutput``) hook: once CI lets the fixer through,
    record the change working before it can finish.

    ``run_demo(round)`` runs the demo agent in its own session, on the run's
    default model credential, and returns its verdict. Each round:

    1. Every worktree has to be clean (the fixer just pushed, so it normally
       is): the stash below has to hold only the demo's edits. A dirty one is
       sent back to the fixer to commit or delete.
    2. Pop the previous round's demo setup back.
    3. Run the demo agent.
    4. Stop every process the round started, then stash whatever it left,
       so the fixer gets its clean checkout back.
    5. ``ok`` passes; any other verdict, or the demo agent failing or running
       past ``DEMO_AGENT_TIMEOUT_S``, is denied back to the fixer.

    Bounded at ``MAX_DEMO_ROUNDS`` rounds, and the fixer can stand it down by
    writing a reason to ``demo_dir / BYPASS_FILE``. ``state`` collects the
    rounds for the runner, which posts the last ``ok`` once the fixer is done.
    Register it after the CI gates, so a CLI that ran hooks one by one would
    still reach them first.
    """

    async def _hook(
        hook_input: PreToolUseHookInput, tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        if hook_input.get("tool_name") != _STRUCTURED_OUTPUT_TOOL:
            return {}
        if ci_gates is not None and await ci_gates.any_denied(_call_id(hook_input, tool_use_id)):
            return {}
        if state.bypass_reason:
            return {}
        if reason := demo_bypass_reason(demo_dir):
            state.bypass_reason = reason
            ctx.emit(Panel(title="Demo Bypass", content=reason, style="yellow"))
            return {}

        pushed = [wt for wt in worktrees if _pushed(wt)]
        if not pushed:
            return {}
        heads = {str(wt.path): head_sha(wt.path) for wt in pushed}
        if state.ok is not None and state.ok.heads == heads:
            return {}
        if state.rounds >= MAX_DEMO_ROUNDS:
            return {}
        dirty = {str(wt.path): status for wt in worktrees if (status := git_status(wt.path))}
        if dirty:
            state.rounds += 1
            state.dirty = sorted(dirty)
            return _deny_structured_output(_dirty_feedback(dirty, demo_dir))

        state.rounds += 1
        state.dirty = []
        reset_demo_dir(demo_dir)
        for wt in worktrees:
            restore_setup(wt.path)

        ctx.emit(ActivityStart(name="demo"))
        started = time.monotonic()
        before = running_pids()
        demo: DemoRound | None = None
        error = ""
        try:
            demo = await asyncio.wait_for(run_demo(state.rounds), DEMO_AGENT_TIMEOUT_S)
        except TimeoutError:
            error = f"It ran past its {DEMO_AGENT_TIMEOUT_S // 60}-minute budget and was stopped."
        except Exception as exc:
            logger.warning("require_demo_hook: demo agent failed", exc_info=True)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            # Before the stash: a server still running would write into the checkout after it.
            await asyncio.to_thread(stop_new_processes, before)
            for wt in worktrees:
                stash_setup(wt.path)
            ctx.emit(
                ActivityEnd(
                    name="demo",
                    ok=demo is not None and demo.verdict == "ok",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        if demo is None:
            state.error = error
            ctx.emit(Panel(title="Demo Failed", content=error, style="yellow"))
            return _deny_structured_output(_crash_feedback(error, state.rounds, demo_dir))

        demo = demo.model_copy(update={"heads": heads})
        state.last = demo
        state.error = ""
        ctx.emit(
            Panel(
                title=f"Demo: {demo.verdict}",
                content=demo.evidence,
                style="green" if demo.verdict == "ok" else "yellow",
            )
        )
        if demo.verdict == "ok":
            state.ok = demo
            return {}
        return _deny_structured_output(_demo_feedback(demo, state.rounds, demo_dir))

    return HookMatcher(matcher=_STRUCTURED_OUTPUT_TOOL, hooks=[_hook], timeout=_DEMO_HOOK_TIMEOUT)


def _git_subcommand(tokens: list[str]) -> str:
    """The subcommand of a ``git`` invocation, past ``-C <dir>``/``-c <k=v>`` and flags."""
    rest = tokens[1:]
    while rest:
        token = rest.pop(0)
        if token in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            if rest:
                rest.pop(0)
            continue
        if token.startswith("-"):
            continue
        return token
    return ""


_FORBIDDEN_GIT = frozenset({"push", "commit", "stash"})


def _publishes(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tool = Path(tokens[0]).name
    if tool == "git":
        return _git_subcommand(tokens) in _FORBIDDEN_GIT
    if tool == "gh":
        return tokens[1:2] == ["pr"]
    if tool == "glab":
        return tokens[1:2] == ["mr"]
    return False


def forbid_publishing_hook() -> HookMatcher:
    """PreToolUse(Bash) hook for the demo agent: no commit, push, stash or PR/MR.

    The stash the demo gate takes after each round is what really keeps the
    demo's setup out of the PR; this stops the agent working against it.
    """

    async def _hook(
        hook_input: PreToolUseHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        if hook_input.get("tool_name") != "Bash":
            return {}
        command = (hook_input.get("tool_input") or {}).get("command") or ""
        if not isinstance(command, str):
            return {}
        if not any(_publishes(segment) for segment in _command_segments(command)):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "The demo agent never commits, pushes, stashes or touches a PR/MR: your "
                    "setup is collected for you after this round and kept out of the change."
                ),
            }
        }

    return HookMatcher(matcher="Bash", hooks=[_hook])


# --------------------------------------------------------------------------
# GitLab threaded replies
# --------------------------------------------------------------------------

# Shell control operators, as they survive shlex tokenization.
_SEPARATOR_TOKENS = frozenset({";", "&&", "||", "|", "&"})

# A note endpoint scoped to an issue/MR rather than to a discussion.
_FLAT_NOTE_PATH_RE = re.compile(r"/(?:issues|merge_requests)/(?P<iid>\d+)/notes(?:\b|$)")

# Sentinel for `glab mr note` with no explicit iid (glab infers it from the
# current branch) — it still targets the mention's own MR.
_IID_UNSPECIFIED = ""

# `glab ... note` flags that consume the following token, so its value is
# never the positional iid.
_VALUE_FLAGS = frozenset({"-m", "--message", "-R", "--repo", "-F", "-f", "--field"})


def _command_segments(command: str) -> list[list[str]]:
    """Split a shell command into per-invocation token lists.

    Tokenizing first, then splitting on control operators, keeps
    separators that appear *inside* quotes (a reply body containing a
    ``|``, say) from splitting the command.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes — bash would reject this too. Nothing to match.
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATOR_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _has_post_method(tokens: list[str]) -> bool:
    """True iff these ``glab api`` tokens request an explicit POST."""
    for i, token in enumerate(tokens):
        if token in ("--method", "-X") and i + 1 < len(tokens) and tokens[i + 1].upper() == "POST":
            return True
        if token.startswith("--method=") and token.split("=", 1)[1].upper() == "POST":
            return True
    return False


def _flat_note_iid(tokens: list[str]) -> str | None:
    """Return the target iid if this invocation posts a flat, unthreaded note.

    ``None`` means "not a flat note post" — including reads (``glab api``
    without POST) and discussion-scoped posts, which are exactly what we
    want the agent doing.
    """
    if not tokens or Path(tokens[0]).name != "glab":
        return None
    rest = tokens[1:]

    # `glab mr note <iid>` / `glab issue note <iid>`
    if len(rest) >= 2 and rest[0] in ("mr", "issue") and rest[1] == "note":
        skip_value = False
        for token in rest[2:]:
            if skip_value:
                skip_value = False
                continue
            if token in _VALUE_FLAGS:
                skip_value = True
                continue
            # The iid is the only bare positional; skipping flag *values*
            # above keeps a numeric reply body from being read as one.
            if token.isdigit():
                return token
        return _IID_UNSPECIFIED

    # `glab api --method POST projects/<slug>/issues/<iid>/notes`
    if rest[:1] == ["api"] and _has_post_method(rest):
        for token in rest[1:]:
            if "/discussions/" in token:
                continue
            match = _FLAT_NOTE_PATH_RE.search(token)
            if match:
                return match["iid"]
    return None


def _posts_flat_note(command: str, parent_iid: str) -> bool:
    """True iff ``command`` posts a flat note on the mention's own issue/MR."""
    return any(
        iid in (parent_iid, _IID_UNSPECIFIED)
        for iid in (_flat_note_iid(segment) for segment in _command_segments(command))
        if iid is not None
    )


def require_threaded_gitlab_reply_hook(mention: MentionContext) -> HookMatcher | None:
    """PreToolUse hook: keep GitLab replies inside the mention's discussion.

    Every GitLab note belongs to a discussion. ``glab issue note`` and
    ``glab mr note`` always open a *new* one, so a reply posted that way
    renders as a disconnected top-level comment instead of nesting under
    the comment that triggered it. Only
    ``POST .../discussions/<id>/notes`` threads correctly (and it also
    promotes a standalone comment into a thread, which is what replying
    to a plain top-level mention needs).

    Two prompt rewrites failed to stop the agent reaching for the flat
    command, so this denies it outright and hands back the exact
    replacement. Returns ``None`` when it can't apply — a non-GitLab
    mention, or no resolved thread id, in which case the flat note is
    genuinely the only option and blocking it would leave the mention
    unanswered.
    """
    if mention.platform != "gitlab" or not mention.thread_id:
        return None
    parent_iid = mention.pr or mention.issue
    if not parent_iid:
        return None

    parent_path = "merge_requests" if mention.pr else "issues"
    replacement = (
        f'glab api --method POST "projects/{quote(mention.repo, safe="")}'
        f'/{parent_path}/{parent_iid}/discussions/{mention.thread_id}/notes" '
        f'-f body="<your reply>"'
    )
    blocks = 0

    async def _hook(
        hook_input: PreToolUseHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        nonlocal blocks
        if blocks >= _MAX_BLOCKS:
            # Give up rather than burn the whole turn: an unthreaded reply
            # still beats leaving the mention unanswered.
            return {}
        if hook_input.get("tool_name") != "Bash":
            return {}
        command = (hook_input.get("tool_input") or {}).get("command") or ""
        if not isinstance(command, str) or not _posts_flat_note(command, parent_iid):
            return {}

        blocks += 1
        logger.warning("Blocked flat GitLab note; steering to discussion %s", mention.thread_id)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "That command posts a flat, top-level GitLab note, which renders as a "
                    "disconnected comment instead of a reply inside the discussion this "
                    f"mention came from (discussion {mention.thread_id}). "
                    "Post it in the discussion instead, keeping your body text as-is:\n\n"
                    f"{replacement}\n\n"
                    "Use that form for every note you post on this "
                    f"{'MR' if mention.pr else 'issue'} this turn, including routing "
                    "announcements and follow-up links."
                ),
            }
        }

    return HookMatcher(matcher="Bash", hooks=[_hook])
