"""SentryFixWorkflow — triage(+plan) → route → (synthesize?) → fix per group."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import ClassVar, Literal

from claude_agent_sdk import ResultError

from src.activities.ci_watch import check_ci
from src.activities.git import (
    PRRef,
    WorktreePath,
    attach_label,
    close_pr,
    create_worktree,
    ensure_repo,
    open_pr,
    pr_number,
    push_branch,
    resolve_target_repos,
    verify_fix_pushed,
)
from src.activities.sentry import (
    GroupResult,
    RoutingPlan,
    SentryEvent,
    SynthesisAgentOutput,
    SynthesisGroup,
    TriagedIssue,
    fetch_sentry_data,
    filter_and_route,
    gitleaks_scan,
)
from src.agents.hooks import bypass_marker_path, require_ci_pass_hook, require_pushed_fix_hook
from src.agents.sentry import (
    FixerAgent,
    FixerInput,
    SynthesisAgent,
    SynthesisInput,
    TriageAgent,
    TriageInput,
)
from src.output import emit_rate_limit_error, extract_retry_after
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.base import register
from src.workflows.schemas import WorkflowResult
from src.workflows.sentry_fix.utils import (
    branch_name,
    detect_platform,
    fallback_groups,
    format_triage_panel,
    pr_body,
    pr_title,
    serialize_triages,
    synthesized_groups,
    triage_from_result,
)

logger = logging.getLogger(__name__)


REVIEW_LABEL = "jeanclode:review"
SUMMARY_LABEL = "jeanclode:summary"


def _repo_result(pr: PRRef, branch: str, *, ci: str | None = None) -> dict[str, str | int]:
    """Serialise one repo's PR outcome for the structured result.

    Carries ``pr_number`` + ``provider`` + ``head_branch`` so the backend can
    address and link the PR without parsing its URL or touching the branch.
    """
    out: dict[str, str | int] = {
        "pr_url": pr.url,
        "provider": pr.platform,
        "head_branch": branch,
    }
    number = pr_number(pr)
    if number is not None:
        out["pr_number"] = number
    if ci is not None:
        out["ci"] = ci
    return out


@register
class SentryFixWorkflow:
    """Python-driven port of the legacy ``sentry-fix`` skill.

    Flow:
      1. Fetch each Sentry issue (deterministic).
      2. Triage in parallel via the LLM agent — triage is also the planner:
         it reads the source to reach its verdict and hands the fixer its
         findings plus the repo(s) the fix belongs in (ADR-007).
      3. Filter and route deterministically.
      4. Synthesize groups when there are multiple actionable issues, so
         issues sharing a root cause land in one PR instead of several.
      5. Per group: a worktree and a PR per target repo, then one
         fixer session across them.
    """

    name: ClassVar[str] = "sentry-fix"
    description: ClassVar[str] = "Triage Sentry errors and open fix PRs"
    triggers: ClassVar[list[str]] = [
        "*sentry.io/issues/*",
        "*sentry*/issues/*",  # self-hosted, e.g. sentry.example.com/issues/*
        "command:sentry",
    ]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        events = fetch_sentry_data(list(ctx.issues), ctx=ctx)
        if not events:
            return WorkflowResult(
                status="error",
                summary="Failed to fetch any Sentry data",
            )

        triages, work_ctx, unparsed = await self._triage_all(events, ctx)
        plan = filter_and_route(triages, ctx=work_ctx, unparsed=unparsed)
        ctx.emit(
            Panel(
                title="Triage Results",
                content=format_triage_panel(plan),
                style="cyan",
            )
        )

        if plan.action == "stop":
            # Two of these are wiring failures, not clean no-ops, and
            # reporting success for either is how a correct triage silently
            # fixes nothing (jc-sentry-1887773): an issue triage called
            # actionable that resolved to no checked-out repo, and a triage
            # whose structured output never made it back.
            broken = bool(plan.unresolved_repo or plan.unparsed)
            status: Literal["success", "error"] = "error" if broken else "success"
            return WorkflowResult(
                status=status,
                summary=plan.reason or "Nothing to fix",
                data={
                    "action": "stop",
                    "triages": serialize_triages(plan),
                    "unresolved_repo": [t.issue_id for t in plan.unresolved_repo],
                    "unparsed": plan.unparsed,
                },
            )

        groups = (
            await self._synthesize(plan, work_ctx) if plan.action == "synthesize" else plan.groups
        )

        results = await asyncio.gather(
            *[self._run_group(g, work_ctx) for g in groups],
            return_exceptions=False,
        )

        prs = [url for r in results if r.ok for url in r.pr_urls]
        ok_count = sum(1 for r in results if r.ok)
        fail_count = len(results) - ok_count
        result_status: Literal["success", "error"] = "success" if ok_count else "error"
        summary = f"{len(prs)} PR(s) opened, {fail_count} group(s) failed"
        if plan.unparsed:
            summary += f", {len(plan.unparsed)} triage(s) returned nothing usable"

        return WorkflowResult(
            status=result_status,
            summary=summary,
            data={
                "action": plan.action,
                "triages": serialize_triages(plan),
                "results": [r.model_dump(mode="json") for r in results],
                "pr_urls": prs,
                "unparsed": plan.unparsed,
            },
        )

    async def _triage_all(
        self, events: list[SentryEvent], ctx: RunContext
    ) -> tuple[list[TriagedIssue], RunContext, list[str]]:
        """Triage every event in parallel and pair with its source URL.

        If the event resolved a repo URL we clone it (when not already
        cloned) and run that event's triage with the repo as cwd, so the
        agent can read the source files to verify the bug. Without this,
        the cwd is an empty workspace and the agent reports lower
        confidence because it can't open the affected files.

        Returns the triaged issues, a context whose ``related_repos``
        also lists anything cloned here (so routing and the group runner
        resolve triage's ``target_repos`` names against every checkout
        that exists on disk, not just the ones preflight knew about), and
        the ids of any issue whose triage came back unusable.
        """
        agent = TriageAgent()
        cloned: dict[str, Path] = {}
        triage_results = await asyncio.gather(
            *(
                agent.invoke(
                    TriageInput(
                        issue_id=e.issue_id,
                        sentry_url=e.sentry_url,
                        formatted=e.formatted,
                    ),
                    self._ctx_for_event(e, ctx, cloned),
                )
                for e in events
            )
        )
        triaged: list[TriagedIssue] = []
        unparsed: list[str] = []
        for event, result in zip(events, triage_results, strict=True):
            triage = triage_from_result(result.structured)
            if triage is None:
                logger.warning(
                    "triage for issue %s returned no usable structured output",
                    event.issue_id,
                )
                unparsed.append(event.issue_id)
                continue
            triaged.append(
                TriagedIssue(
                    issue_id=event.issue_id,
                    sentry_url=event.sentry_url,
                    triage=triage,
                )
            )
        return triaged, self._with_cloned_repos(ctx, cloned), unparsed

    def _ctx_for_event(
        self, event: SentryEvent, ctx: RunContext, cloned: dict[str, Path]
    ) -> RunContext:
        """Return a ctx whose cwd is the cloned repo for this event, if any."""
        if not event.repo_url:
            return ctx
        try:
            repo_dir = ensure_repo(event.repo_url, ctx=ctx)
        except Exception:
            logger.warning(
                "ensure_repo failed for %s; triaging without source",
                event.repo_url,
                exc_info=True,
            )
            return ctx
        cloned[repo_dir.name] = repo_dir
        return ctx.with_cwd(repo_dir)

    @staticmethod
    def _with_cloned_repos(ctx: RunContext, cloned: dict[str, Path]) -> RunContext:
        known = {ctx.cwd.name} | {r["name"] for r in ctx.related_repos}
        extra = [
            {"name": name, "path": str(path)} for name, path in cloned.items() if name not in known
        ]
        if not extra:
            return ctx
        return ctx.model_copy(update={"related_repos": [*ctx.related_repos, *extra]})

    async def _synthesize(self, plan: RoutingPlan, ctx: RunContext) -> list[SynthesisGroup]:
        result = await SynthesisAgent().invoke(
            SynthesisInput(actionable=plan.actionable),
            ctx,
        )
        parsed = SynthesisAgentOutput.model_validate(result.structured or {})
        if not parsed.groups:
            # Safe degrade — one group per issue — but never a quiet one:
            # SynthesisAgentOutput nests a model, so its schema can't be
            # sent in the CLI's strict dialect and a malformed payload
            # reaches us looking like "no groups" (see
            # BaseAgent._schema_for_cli).
            logger.warning(
                "synthesis agent returned no groups; treating each of the %d "
                "actionable issue(s) as its own group",
                len(plan.actionable),
            )
            return fallback_groups(plan.actionable)
        return synthesized_groups(parsed.groups, plan.actionable)

    async def _run_group(self, group: SynthesisGroup, ctx: RunContext) -> GroupResult:
        """Open a PR per target repo, then run one fixer across them.

        Every git and PR command for a repo runs from that repo's own
        worktree path — never from the clone it was branched off, and
        never from another repo's worktree. `glab mr create` infers the
        source branch from the checkout it runs in, so a cwd that doesn't
        match the intended branch opens an MR from the wrong one instead
        of failing loudly.
        """
        branch = branch_name(group)
        targets = resolve_target_repos(ctx, ctx, group.target_repos, allow_primary_fallback=False)
        if not targets:
            return GroupResult(
                group=group,
                ok=False,
                error=f"no checked-out repo matched target_repos {group.target_repos}",
            )
        multi = len(targets) > 1
        parent_dir = ctx.workspace / "worktrees" / branch.replace("/", "_")

        worktrees: dict[str, WorktreePath] = {}
        prs: dict[str, PRRef] = {}
        try:
            gitleaks_scan(pr_body(group), ctx=ctx)

            for name, target_ctx in targets:
                dest = (parent_dir / name) if multi else parent_dir
                wt = create_worktree(branch, ctx=target_ctx, dest=dest)
                worktrees[name] = wt
                # Everything below addresses this repo through its own
                # worktree — the branch these commands act on is the one
                # create_worktree just checked out there.
                wt_ctx = ctx.with_cwd(wt.path)
                push_branch(branch, ctx=wt_ctx)
                siblings = [n for n, _ in targets if n != name]
                prs[name] = open_pr(
                    branch,
                    pr_title(group, name if multi else ""),
                    pr_body(group, siblings),
                    detect_platform(wt_ctx),
                    ctx=wt_ctx,
                )
                wt_ctx.emit(
                    Panel(
                        title="PR Opened",
                        content=f"[bold]{prs[name].url}[/] [dim]({name})[/]",
                        style="green",
                    )
                )

            fixer_cwd = parent_dir if multi else next(iter(worktrees.values())).path
            stop_hooks = []
            pretool_hooks = []
            for name, _target_ctx in targets:
                wt = worktrees[name]
                stop_hooks.append(require_pushed_fix_hook(wt.path, branch, wt.placeholder_sha))
                # CI verification gates the fixer's StructuredOutput call,
                # not Stop — a Stop block lands after the schema-bound
                # fixer has already finalized its turn and the CLI drops
                # it (see src/agents/hooks.py module docstring / ADR-008).
                pretool_hooks.append(
                    require_ci_pass_hook(
                        wt.path,
                        branch,
                        wt.placeholder_sha,
                        prs[name],
                        ctx=ctx.with_cwd(wt.path),
                    )
                )

            await FixerAgent().invoke(
                FixerInput(
                    sentry_urls=group.sentry_urls,
                    branch=branch,
                    findings=group.findings or group.root_cause,
                    repos=[
                        {
                            "name": name,
                            "path": str(worktrees[name].path),
                            "pr_url": prs[name].url,
                            "ci_bypass_path": str(bypass_marker_path(worktrees[name].path)),
                        }
                        for name, _ in targets
                    ],
                ),
                ctx.with_cwd(fixer_cwd),
                extra_hooks={"Stop": stop_hooks, "PreToolUse": pretool_hooks},
            )

            # Per repo: a real pushed commit always gets its PR marked
            # ready and labeled for review, whether or not CI ended up
            # green — the CI gate hook already gave the fixer its retry
            # budget to fix anything it broke (require_ci_pass_hook); a
            # failure that survives that isn't grounds to leave the work
            # sitting unreviewed, and it isn't a run failure either —
            # "error" here means the run itself broke (nothing pushed, an
            # exception), not "the generated fix didn't pass CI".
            # ci_result is recorded per repo for visibility only.
            repo_results: dict[str, dict[str, str | int]] = {}
            for name, _target_ctx in targets:
                wt = worktrees[name]
                wt_ctx = ctx.with_cwd(wt.path)
                missing = verify_fix_pushed(branch, wt.placeholder_sha, ctx=wt_ctx)
                if missing:
                    # Nothing landed here. Expected when triage listed a
                    # repo that turned out not to need a change — but the
                    # PR is already open (unlike issue-resolve, sentry
                    # opens before the fixer runs), so close it rather
                    # than leave an empty MR sitting on the repo.
                    try:
                        close_pr(
                            prs[name],
                            ctx=wt_ctx,
                            comment="No change was needed in this repo — closing this PR.",
                        )
                    except Exception:
                        # Tidying up an empty PR must never cost the
                        # group the fixes that did land in its other repos.
                        logger.warning("failed to close unused PR %s", prs[name].url, exc_info=True)
                    continue
                ci_result = await asyncio.to_thread(check_ci, prs[name], cwd=wt.path)
                attach_label(prs[name], REVIEW_LABEL, ctx=wt_ctx)
                attach_label(prs[name], SUMMARY_LABEL, ctx=wt_ctx)
                repo_results[name] = _repo_result(prs[name], branch, ci=ci_result.outcome)

            if not repo_results:
                return GroupResult(
                    group=group,
                    ok=False,
                    error="fixer agent finished without pushing a real change to any repo",
                )

            return GroupResult(group=group, repos=repo_results, ok=True)
        except Exception as exc:
            logger.warning("group %s failed: %s", "+".join(group.issue_ids), exc, exc_info=True)
            # A rate-limit hit here is swallowed by this same except block
            # like any other failure — this group's own retry loop
            # (branch_name/push_branch/open_pr already deliberately
            # reuse the branch/PR on a per-group retry) isn't what handles
            # it. The *watcher* does: it needs the branch/PRs that were in
            # scope for this specific group, only known here, to close the
            # leftover PRs and scrub their Sentry-issue-identifying
            # bodies before the workflow restarts from triage (ADR-010) —
            # without that cleanup, TriageAgent's existing-PR search would
            # match the leftover PR and treat the issue as already handled.
            # One event per PR: the watcher cleans up whatever it's told
            # about, and a multi-repo group leaves one PR per repo.
            if (
                isinstance(exc, ResultError)
                and exc.api_error_status == 429
                and not sys.stderr.isatty()
            ):
                for pr_url in [pr.url for pr in prs.values()] or [""]:
                    emit_rate_limit_error(
                        str(exc),
                        retry_after=extract_retry_after(exc),
                        branch=branch,
                        pr_url=pr_url,
                    )
            return GroupResult(
                group=group,
                repos={name: _repo_result(pr, branch) for name, pr in prs.items()},
                ok=False,
                error=str(exc),
            )
