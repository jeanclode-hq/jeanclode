from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from src.activities.notify import post_ready_notice
from src.activities.review import (
    apply_guardrail,
    dedup_against_existing_comments,
    filter_and_route,
    post_bot_followup,
    post_comments,
    recover_failed_post,
    style_comments,
)
from src.activities.review.schemas import Comment, PRContext, PRRef
from src.agents.review import (
    AnalyzerAgent,
    AnalyzerInput,
    DeduplicatorAgent,
    DeduplicatorInput,
    FactCheckerAgent,
    FactCheckerInput,
    IssueExplorerAgent,
    IssueExplorerInput,
    StylerAgent,
    StylerInput,
    SynthesizerAgent,
    SynthesizerInput,
)
from src.agents.schemas import AgentResult
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.base import register
from src.workflows.code_review.utils import (
    comment_anchor,
    dry_run_panel,
    load_pr_context,
    parse_keep_indices,
    parse_review,
    parse_styled_bodies,
    serialize_comments,
    serialize_numbered,
)
from src.workflows.schemas import WorkflowResult

logger = logging.getLogger(__name__)


@register
class CodeReviewWorkflow:
    name: ClassVar[str] = "code-review"
    description: ClassVar[str] = "Review a GitHub PR or GitLab MR and post inline comments"
    triggers: ClassVar[list[str]] = [
        "github.com/*/pull/*",
        "*.github.com/*/pull/*",
        "gitlab.com/*/-/merge_requests/*",
        "*/-/merge_requests/*",
        "command:review",
    ]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        prc = load_pr_context(ctx.cwd)
        if prc is None:
            return WorkflowResult(
                status="error",
                summary="missing or invalid .context — cannot run review",
            )

        decision = filter_and_route(ctx=ctx)
        if decision.action == "stop":
            ctx.emit(
                Panel(
                    title="Skipped",
                    content=f"[yellow]{decision.reason}[/]",
                    style="yellow",
                )
            )
            return WorkflowResult(
                status="success",
                summary=f"skipped: {decision.reason}",
                data={"action": "stop", "reason": decision.reason},
            )

        issue_context = await self._explore_issues(ctx, prc)

        analyzer_results = await self._run_analyzers(ctx, prc, issue_context)
        if not analyzer_results:
            # Nothing was actually reviewed; posting LGTM here would claim a
            # clean review that never happened.
            return WorkflowResult(
                status="error",
                summary="every analyzer failed — no review performed",
            )
        merged: list[Comment] = []
        for r in analyzer_results:
            merged.extend(parse_review(r, prc.ref.platform))
        if not merged:
            return await self._finalize_post(ctx, prc.ref, [])

        synth = await SynthesizerAgent().invoke(
            SynthesizerInput(
                platform=prc.ref.platform,
                pr_description=prc.pr_description,
                diff=prc.diff,
                findings_json=serialize_comments(merged),
                issue_context=issue_context,
            ),
            ctx,
        )
        comments = parse_review(synth, prc.ref.platform)
        if not comments:
            return await self._finalize_post(ctx, prc.ref, [])

        comments = await self._maybe_dedup(ctx, prc, comments)
        if not comments:
            return await self._finalize_post(ctx, prc.ref, [])

        comments = await self._fact_check(ctx, prc, comments, issue_context)
        if not comments:
            return await self._finalize_post(ctx, prc.ref, [])

        comments = apply_guardrail(comments, ctx=ctx)
        if not comments:
            return await self._finalize_post(ctx, prc.ref, [])

        comments = await self._style(ctx, comments)
        return await self._finalize_post(ctx, prc.ref, comments)

    async def _explore_issues(self, ctx: RunContext, prc: PRContext) -> str:
        try:
            result = await IssueExplorerAgent().invoke(
                IssueExplorerInput(
                    platform=prc.ref.platform,
                    repo=prc.ref.repo,
                    pr_description=prc.pr_description,
                ),
                ctx,
            )
        except Exception:
            logger.warning("issue explorer failed", exc_info=True)
            return ""
        text = (result.text or "").strip()
        if result.structured and "context" in result.structured:
            ctx_str = result.structured.get("context", "")
            if isinstance(ctx_str, str):
                text = ctx_str.strip()
        if "no linked issues" in text.lower():
            return ""
        return text

    async def _run_analyzers(
        self, ctx: RunContext, prc: PRContext, issue_context: str
    ) -> list[AgentResult]:
        a0 = AnalyzerAgent()
        a1 = AnalyzerAgent()
        a0.name = "Analyzer[0]"  # type: ignore[misc]
        a1.name = "Analyzer[1]"  # type: ignore[misc]
        agent_input = AnalyzerInput(
            platform=prc.ref.platform,
            pr_description=prc.pr_description,
            diff=prc.diff,
            issue_context=issue_context,
        )
        raw = await asyncio.gather(
            a0.invoke(agent_input, ctx),
            a1.invoke(agent_input, ctx),
            return_exceptions=True,
        )
        results: list[AgentResult] = []
        for r in raw:
            if isinstance(r, BaseException):
                logger.warning("analyzer failed: %s", r)
                continue
            results.append(r)
        return results

    async def _maybe_dedup(
        self, ctx: RunContext, prc: PRContext, comments: list[Comment]
    ) -> list[Comment]:
        body = prc.discussions
        if not body.strip() or ("_None._" in body and body.count("_None._") >= 3):
            return comments
        try:
            result = await DeduplicatorAgent().invoke(
                DeduplicatorInput(
                    discussions=body,
                    comments_json=serialize_numbered(comments),
                ),
                ctx,
            )
        except Exception:
            logger.warning("deduplicator failed; keeping all", exc_info=True)
            return comments
        return dedup_against_existing_comments(comments, parse_keep_indices(result), ctx=ctx)

    async def _fact_check(
        self,
        ctx: RunContext,
        prc: PRContext,
        comments: list[Comment],
        issue_context: str,
    ) -> list[Comment]:
        try:
            result = await FactCheckerAgent().invoke(
                FactCheckerInput(
                    pr_description=prc.pr_description,
                    diff=prc.diff,
                    comments_json=serialize_numbered(comments),
                    issue_context=issue_context,
                ),
                ctx,
            )
        except Exception:
            logger.warning("fact-checker failed; keeping all", exc_info=True)
            return comments
        return dedup_against_existing_comments(comments, parse_keep_indices(result), ctx=ctx)

    async def _style(self, ctx: RunContext, comments: list[Comment]) -> list[Comment]:
        try:
            result = await StylerAgent().invoke(
                StylerInput(comments_json=serialize_numbered(comments)),
                ctx,
            )
        except Exception:
            logger.warning("styler failed; keeping originals", exc_info=True)
            return comments
        return style_comments(comments, parse_styled_bodies(result), ctx=ctx)

    async def _finalize_post(
        self,
        ctx: RunContext,
        pr_ref: PRRef,
        comments: list[Comment],
    ) -> WorkflowResult:
        if ctx.dry_run:
            ctx.emit(
                Panel(
                    title="Dry Run",
                    content=dry_run_panel(comments, pr_ref),
                    style="yellow",
                )
            )
            return WorkflowResult(
                status="success",
                summary=f"dry-run: {len(comments)} comments not posted",
                data={"dry_run": True, "comments": [c.model_dump() for c in comments]},
            )
        post = post_comments(comments, pr_ref, ctx=ctx)
        recover = None
        if post.unposted:
            recover = recover_failed_post(post.unposted, pr_ref, ctx=ctx)

        notified = False
        if pr_ref.author_is_bot and not post.lgtm:
            followup_ok, followup_err = post_bot_followup(pr_ref, ctx=ctx)
            post.loop_triggered = followup_ok
            if not followup_ok and followup_err:
                post.errors.append(f"bot follow-up: {followup_err}")
        elif pr_ref.author_is_bot and post.lgtm:
            # LGTM on a bot-opened PR is the loop's exit: review found
            # nothing left to fix, so this is the first moment the MR is
            # in final shape and worth a human's attention.
            notified = post_ready_notice(
                pr_ref.platform,
                pr_ref.repo,
                pr_ref.pr,
                ctx.notify_users,
                pr_url=pr_ref.pr_url or post.pr_url,
                ctx=ctx,
            )

        if post.lgtm:
            ctx.emit(Panel(title="LGTM", content=post.pr_url, style="green"))
        else:
            for c in comments:
                ctx.emit(
                    Panel(title=comment_anchor(c), content=c.body, style="cyan"),
                )
            if recover and recover.error:
                ctx.emit(
                    Panel(
                        title="Top-level fallback failed",
                        content=recover.error,
                        style="red",
                    ),
                )

        summary = self._summarize(post, recover, comments)
        data: dict[str, object] = {
            "post": post.model_dump(),
            "comments_posted": post.posted,
            "lgtm": post.lgtm,
            "notified": notified,
        }
        if recover is not None:
            data["recover"] = recover.model_dump()
        ok = (
            post.posted > 0
            or post.lgtm
            or (recover is not None and recover.recovered > 0 and not recover.error)
        )
        return WorkflowResult(
            status="success" if ok else "error",
            summary=summary,
            data=data,
        )

    @staticmethod
    def _summarize(post, recover, comments) -> str:
        if post.lgtm:
            return "LGTM"
        if not comments:
            return "no findings"
        recovered = recover.recovered if recover is not None else 0
        bits = [f"{post.posted} posted"]
        if post.failed:
            bits.append(f"{post.failed} failed")
        if recovered:
            bits.append(f"{recovered} recovered top-level")
        return ", ".join(bits)
