"""PRSummaryWorkflow — generate a summary and write it into the PR/MR description.

Sequential pipeline:
    summarizer → styler → format_summary → update_pr_description

Two agents, not three. A separate issue-explorer stage used to fetch every
linked GitHub/GitLab issue for context and hand back a list of issue URLs
to render as a section — but the description it summarizes already states
what this change relates to, and the relationship ("closes", "depends on",
"the Sentry error this fixes") is what a reader wants, not a bare list.
The summarizer keeps those links in its own prose instead.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from src.activities.summary import (
    ParsedSummary,
    PRSnapshot,
    format_summary,
    update_pr_description,
)
from src.agents.summary import (
    ParserAgent,
    ParserInput,
    SummarizerAgent,
    SummarizerInput,
)
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.base import register
from src.workflows.pr_summary.utils import (
    load_pr_snapshot,
    panel_style,
    panel_title,
    parse_description,
    result_panel,
)
from src.workflows.schemas import WorkflowResult

logger = logging.getLogger(__name__)


@register
class PRSummaryWorkflow:
    name: ClassVar[str] = "pr-summary"
    description: ClassVar[str] = "Generate and post a PR/MR summary comment"
    triggers: ClassVar[list[str]] = ["command:summary"]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        snapshot = load_pr_snapshot(ctx.cwd)
        if snapshot is None:
            return WorkflowResult(
                status="error",
                summary="missing or invalid .context — cannot run summary",
            )

        draft = await self._summarize(ctx, snapshot)
        if not draft:
            return WorkflowResult(
                status="error",
                summary="summarizer produced no draft — cannot continue",
            )
        refined = await self._refine(ctx, draft)

        payload = format_summary(ParsedSummary(description=refined), ctx=ctx)
        return await self._finalize(ctx, snapshot, payload)

    async def _summarize(self, ctx: RunContext, snap: PRSnapshot) -> str:
        try:
            result = await SummarizerAgent().invoke(
                SummarizerInput(
                    pr_description=snap.pr_description,
                    diff=snap.diff,
                ),
                ctx,
            )
        except Exception:
            logger.warning("summarizer failed", exc_info=True)
            return ""
        return parse_description(result)

    async def _refine(self, ctx: RunContext, draft: str) -> str:
        try:
            result = await ParserAgent().invoke(ParserInput(draft=draft), ctx)
        except Exception:
            logger.warning("parser failed; using draft as-is", exc_info=True)
            return draft
        refined = parse_description(result)
        return refined or draft

    async def _finalize(self, ctx: RunContext, snap: PRSnapshot, payload) -> WorkflowResult:
        if ctx.dry_run:
            ctx.emit(
                Panel(
                    title="Summary (dry-run)",
                    content=payload.body,
                    style="yellow",
                )
            )
            return WorkflowResult(
                status="success",
                summary=f"dry-run: would update description on {snap.pr_url}",
                data={"posted": False, "pr_url": snap.pr_url, "body": payload.body},
            )

        post = update_pr_description(payload, snap, ctx=ctx)
        ctx.emit(
            Panel(
                title=panel_title(post),
                content=result_panel(post, payload, snap),
                style=panel_style(post),
            )
        )
        summary = (
            f"Updated PR description: {post.pr_url}"
            if post.posted
            else f"Failed to update PR description: {post.error}"
        )
        return WorkflowResult(
            status="success" if post.posted else "error",
            summary=summary,
            data={"post": post.model_dump(), "posted": post.posted, "pr_url": post.pr_url},
        )
