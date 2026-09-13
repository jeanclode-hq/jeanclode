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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from claude_agent_sdk import HookContext, HookMatcher, PreToolUseHookInput, StopHookInput

from src.activities.ci_watch import check_ci
from src.activities.git.ops import fix_missing_reason
from src.activities.git.schemas import PRRef
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

# The SDK-internal tool the agent calls to emit its `output_schema` result.
# It's how a schema-bound agent finalizes its turn, so gating it is
# equivalent to gating "the agent is about to declare itself done" — but
# early enough that a `deny` still lands (see module docstring).
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

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
        if blocks >= _MAX_BLOCKS:
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

    # check_ci's own wait loop can run up to ~495s (settle + wait-timeout,
    # see poll.py) — the SDK's HookMatcher default timeout is only 60s,
    # which would kill this hook's callback long before check_ci returns.
    return HookMatcher(matcher=_STRUCTURED_OUTPUT_TOOL, hooks=[_hook], timeout=600)


def require_pushed_and_ci_pass_hook(
    cwd: Path,
    branch: str,
    placeholder_sha: str,
    open_pr: Callable[[], PRRef],
    *,
    ctx: RunContext,
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
    """
    blocks = 0
    pr: PRRef | None = None

    async def _hook(
        hook_input: PreToolUseHookInput, _tool_use_id: str | None, _context: HookContext
    ) -> dict[str, Any]:
        nonlocal blocks, pr
        if hook_input.get("tool_name") != _STRUCTURED_OUTPUT_TOOL:
            return {}
        if blocks >= _MAX_BLOCKS:
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

    return HookMatcher(matcher=_STRUCTURED_OUTPUT_TOOL, hooks=[_hook], timeout=600)


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
