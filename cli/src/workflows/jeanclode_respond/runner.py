"""JeanclodeRespondWorkflow — invoke the planner, then close the loop.

The planner agent executes every action itself via Bash (``gh`` /
``glab`` / ``git``), including deciding whether to push a code change.
The Python-side follow-up is two deterministic, judgment-free checks
against provider state before and after the turn — never the planner's
own account of what it did:

* the branch's tip on origin moved → re-attach ``jeanclode:review`` so
  the new commits get reviewed. Python never pushes; whether/when to
  push is entirely the planner's call.
* nothing was pushed but the last open thread closed → the review loop
  has converged with no further round to run, so this is where the
  ready notice goes. Without it, a turn that resolves every finding as
  a false positive ends the loop silently: no push, no relabel, no
  re-review, no LGTM, and nobody told the MR is done.

Two PreToolUse hooks constrain the turn: one keeps GitLab replies inside
the discussion the mention came from, the other verifies the repo's CI on
a turn that pushed code, before the planner is allowed to finalize (see
``agents.hooks``). The planner may well have already replied by then —
that's deliberate, the relabel below sends the fixed push back through
review anyway, and a still-red result never blocks it.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookEvent

from src.activities.notify import post_ready_notice
from src.activities.respond import (
    MentionContext,
    count_unresolved_threads,
    relabel_pr,
)
from src.agents.hooks import require_ci_pass_hook, require_threaded_gitlab_reply_hook
from src.agents.respond import PlannerAgent, PlannerInput
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.base import register
from src.workflows.jeanclode_respond.utils import (
    branch_was_pushed,
    load_mention_context,
    load_pr_snapshot,
    parse_planner_output,
    pr_ref_from_mention,
    read_current_branch,
    read_current_sha,
)
from src.workflows.schemas import WorkflowResult

logger = logging.getLogger(__name__)


@register
class JeanclodeRespondWorkflow:
    name: ClassVar[str] = "jeanclode-respond"
    description: ClassVar[str] = "Handle @jeanclode mentions on PRs/MRs/threads/issues"
    triggers: ClassVar[list[str]] = ["command:respond", "mention:@jeanclode"]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        mention = load_mention_context(ctx.cwd)
        if mention is None:
            return WorkflowResult(
                status="error",
                summary="missing or invalid .context — cannot run respond",
            )

        if ctx.dry_run:
            ctx.emit(Panel(title="Dry Run", content="planner not invoked", style="yellow"))
            return WorkflowResult(
                status="success",
                summary=f"dry-run: would invoke planner on {mention.surface}",
                data={"dry_run": True},
            )

        # Baseline for the post-turn push check. Only meaningful when the
        # mention is on a PR/MR — issue mentions have no branch to compare.
        starting_sha = read_current_sha(ctx.cwd) if mention.pr else ""
        branch = read_current_branch(ctx.cwd) if mention.pr else ""
        # Baseline for the converged-without-a-push check below. Only read
        # when it could possibly fire, so an ordinary mention doesn't pay
        # for two extra API round-trips.
        unresolved_before = (
            count_unresolved_threads(
                mention.platform,
                mention.repo,
                mention.pr,
                target_url=mention.target_url,
                ctx=ctx,
            )
            if self._can_notify(mention, ctx)
            else None
        )

        planner_input = self._planner_input(mention, ctx)
        pretool: list[HookMatcher] = []
        # Deterministic guard: the planner types its own `glab` commands, and
        # the flat note shortcuts silently break threading. No-op unless this
        # is a GitLab mention with a resolved discussion id.
        threaded_reply = require_threaded_gitlab_reply_hook(mention)
        if threaded_reply:
            pretool.append(threaded_reply)
        # Passing starting_sha as the placeholder is what makes this no-op on a
        # turn that pushed nothing — the hook skips its check when HEAD hasn't moved.
        ci_pr = pr_ref_from_mention(mention, branch)
        if ci_pr:
            pretool.append(require_ci_pass_hook(ctx.cwd, branch, starting_sha, ci_pr, ctx=ctx))
        extra_hooks: dict[HookEvent, list[HookMatcher]] | None = (
            {"PreToolUse": pretool} if pretool else None
        )
        result = await PlannerAgent().invoke(planner_input, ctx, extra_hooks=extra_hooks)
        output = parse_planner_output(result)
        if output is None:
            return WorkflowResult(
                status="error",
                summary="planner returned no action",
                data={"raw_text": result.text},
            )

        ctx.emit(
            Panel(
                title=f"Planner: {', '.join(output.actions_taken)}",
                content=output.summary or "(no summary)",
                style="cyan",
            )
        )

        relabeled = False
        notified = False
        if branch_was_pushed(ctx.cwd, starting_sha):
            relabel_result = relabel_pr("jeanclode:review", mention, ctx=ctx)
            relabeled = relabel_result.relabeled
            ctx.emit(
                Panel(
                    title="Re-labelled for review",
                    content="new commits landed on origin during this turn",
                    style="green",
                )
            )
        elif unresolved_before:
            # `unresolved_before` being falsy covers both "couldn't read it"
            # (None) and "nothing was open anyway" (0) — neither is a loop
            # this turn converged, so neither pings.
            unresolved_after = count_unresolved_threads(
                mention.platform,
                mention.repo,
                mention.pr,
                target_url=mention.target_url,
                ctx=ctx,
            )
            if unresolved_after == 0:
                notified = post_ready_notice(
                    mention.platform,
                    mention.repo,
                    mention.pr,
                    ctx.notify_users,
                    pr_url=mention.target_url,
                    ctx=ctx,
                )
                if notified:
                    ctx.emit(
                        Panel(
                            title="Reviewers notified",
                            content="last open thread resolved without a push",
                            style="green",
                        )
                    )

        return WorkflowResult(
            status="success",
            summary=output.summary or ", ".join(output.actions_taken),
            data={
                "actions_taken": output.actions_taken,
                "relabeled": relabeled,
                "notified": notified,
            },
        )

    @staticmethod
    def _can_notify(mention: MentionContext, ctx: RunContext) -> bool:
        """Whether a ready notice could fire for this mention at all.

        ``author_is_bot`` is the gate that matters: respond runs on any
        human's mention, and a human-authored MR where someone asked the
        bot to resolve a thread is not Jeanclode finishing its own work.
        """
        return bool(mention.pr) and bool(ctx.notify_users) and mention.author_is_bot

    @staticmethod
    def _planner_input(mention: MentionContext, ctx: RunContext) -> PlannerInput:
        pr_description, diff, discussions = load_pr_snapshot(ctx.cwd)
        return PlannerInput(
            platform=mention.platform,
            repo=mention.repo,
            pr=mention.pr,
            issue=mention.issue,
            surface=mention.surface,
            target_url=mention.target_url,
            mention_body=mention.mention_body,
            mention_author=mention.mention_author,
            thread_id=mention.thread_id,
            comment_id=mention.comment_id,
            pr_author=mention.pr_author,
            pr_description=pr_description,
            diff=diff,
            discussions=discussions,
        )
