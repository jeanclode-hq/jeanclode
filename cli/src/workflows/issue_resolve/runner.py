"""IssueResolveWorkflow — triage → fix → PR(s) for any git issue.

Flow:
  1. Parse the issue URL to determine provider, repo, and issue number.
  2. Clone the repo via ensure_repo so agents can read the codebase.
  3. Fetch issue context (body, comments) via gh/glab.
  4. Run the triage agent — produces one of 7 outcomes. For "proceed", it
     also investigates the codebase directly (including any repo group the
     issue's repo belongs to) and returns findings (root cause, relevant
     files, suggested approach) plus target_repos: the complete list of
     repo names that need changes, primary included only if it's actually
     one of them — e.g. a fix spanning a frontend and backend repo, or one
     that lives entirely in a related repo while the primary sits untouched
     (see resolve_target_repos).
  5. If outcome != proceed: post a comment on the issue and stop. If
     proceed with code_change=False (the issue asks for an answer, not
     code): the fixer runs in the clone with no worktree, branch, push gate
     or PR, and its comment_body is posted on the issue.
  6. If proceed: one worktree per resolved repo (siblings under one parent
     when there's more than one), a single FixerAgent session spanning all
     of them, implementing straight from triage's findings.
  7. Per repo: once a real commit is pushed there, its PR is opened (not
     before — see require_pushed_and_ci_pass_hook), which also gives the
     fixer a bounded number of fix-and-recheck rounds against that repo's
     own CI. Once that's done — green, still red, or nothing configured —
     the PR is labeled for review/summary regardless, and the run reports
     "success": CI is a signal for the fixer to act on mid-session, not a
     gate on the PR or on the run's own status — "error" is reserved for
     the run itself breaking (nothing pushed anywhere, an exception), not
     for a generated fix that didn't pass CI. check_ci's outcome is still
     recorded per repo for visibility. A target repo the fixer never
     touches (triage's prediction can miss per-repo, same as it can miss
     entirely) just has no worktree activity — no PR, no error.

There's deliberately no separate planning stage: triage already reads the
codebase to decide "proceed", so a second agent re-deriving the same plan
from a cold context was pure redundant exploration. This is specific to
issue-resolve — the sentry-fix pipeline (ADR-007) is unaffected and keeps
its own triage → plan → fix stages, including its eager draft-PR pattern
(open before the fixer runs) — issue-resolve doesn't, deliberately: eagerly
opening a PR per target repo before any of them has real content multiplies
the empty/wrong-PR risk a single eager PR already has (see the "PR opened,
fixer wrote nothing" incident this replaced — jc-gitlab-3c92aaf4).

Re-entry always restarts at triage — no mid-pipeline resume.
Provider-specific CLI choice (gh vs glab) is handled inside activities,
not in this runner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import ClassVar

from src.activities.ci_watch import check_ci
from src.activities.git import (
    PRRef,
    WorktreePath,
    attach_label,
    create_worktree,
    ensure_repo,
    open_pr,
    push_branch,
    resolve_target_repos,
    verify_fix_pushed,
)
from src.activities.issue import IssueContext, fetch_issue_context, post_issue_comment
from src.agents.hooks import (
    bypass_marker_path,
    require_pushed_and_ci_pass_hook,
    require_pushed_fix_hook,
)
from src.agents.issue import (
    IssueFixerAgent,
    IssueFixerInput,
    TriageAgent,
    TriageInput,
)
from src.agents.issue.schemas import TriageOutput
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.runtime.llm_options import FixerLLMChoice, apply_fixer_llm, resolve_fixer_llm
from src.workflows.base import register
from src.workflows.issue_resolve.utils import (
    format_triage_panel,
    issue_branch,
    parse_fixer_output,
    parse_issue_url_info,
    parse_triage_output,
    pr_body,
    pr_title,
    provider_to_platform,
    repo_url_from_parts,
)
from src.workflows.schemas import WorkflowResult

logger = logging.getLogger(__name__)

REVIEW_LABEL = "jeanclode:review"
SUMMARY_LABEL = "jeanclode:summary"


@register
class IssueResolveWorkflow:
    """Triage a git issue then fix and open a PR if actionable.

    Triggered by ``jeanclode issue-resolve <issue_url>`` or automatically
    when a GitHub / GitLab issue URL is passed without an explicit command.
    Works identically for GitHub and GitLab — provider detection is driven
    by the URL, not by hardcoded branching.
    """

    name: ClassVar[str] = "issue-resolve"
    description: ClassVar[str] = "Triage a git issue and open a fix PR"
    triggers: ClassVar[list[str]] = [
        "command:issue-resolve",
        "github.com/*/issues/*",
        "gitlab.com/*/issues/*",
        "*.gitlab.*/*/issues/*",
        # Self-hosted instances are almost always "gitlab.<tld>" (gitlab as
        # the *first* label, e.g. gitlab.example.dev) — "*.gitlab.*"
        # requires a literal "." before "gitlab" and never matches that.
        "gitlab.*/*/issues/*",
        "gitlab.com/*/work_items/*",
        "*.gitlab.*/*/work_items/*",
        "gitlab.*/*/work_items/*",
    ]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        issue_url = ctx.issues[0] if ctx.issues else ""
        if not issue_url:
            return WorkflowResult(status="error", summary="No issue URL provided")

        try:
            provider, repo, issue_number = parse_issue_url_info(issue_url)
        except ValueError as exc:
            return WorkflowResult(status="error", summary=str(exc))

        repo_url = repo_url_from_parts(provider, repo)
        try:
            repo_dir = ensure_repo(repo_url, ctx=ctx)
            repo_ctx = ctx.with_cwd(repo_dir)
        except Exception:
            logger.warning(
                "ensure_repo failed for %s; triaging without cloned repo", repo_url, exc_info=True
            )
            repo_ctx = ctx

        issue_ctx = fetch_issue_context(
            issue_url,
            provider,
            repo,
            issue_number,
            ctx=ctx,
        )

        triage_result = await TriageAgent().invoke(
            TriageInput(
                issue_url=issue_url,
                provider=provider,
                repo=repo,
                repo_name=repo_ctx.cwd.name,
                issue_number=issue_number,
                issue_title=issue_ctx.issue_title,
                issue_body=issue_ctx.issue_body,
                comments=issue_ctx.comments,
            ),
            repo_ctx,
        )

        output = parse_triage_output(triage_result)
        if output is None:
            return WorkflowResult(
                status="error",
                summary="triage agent returned no valid output",
                data={"raw_text": triage_result.text},
            )

        ctx.emit(
            Panel(
                title="Triage Results",
                content=format_triage_panel(output, issue_url),
                style="cyan",
            )
        )

        if output.kind != "proceed":
            if output.comment_body:
                _post_comment(output.comment_body, issue_url, provider, repo, issue_number, ctx)
            return WorkflowResult(
                status="success",
                summary=f"triage: {output.kind}",
                data={
                    "kind": output.kind,
                    "triage_result": "not_actionable",
                    "reasoning": output.reasoning,
                },
            )

        fixer_llm = resolve_fixer_llm(
            ctx.llm_options,
            output.fixer_llm_credential,
            output.fixer_llm_tier,
            output.fixer_llm_reason,
        )
        if fixer_llm.summary:
            ctx.emit(Panel(title="Fixer LLM", content=fixer_llm.summary, style="cyan"))

        if not output.code_change:
            return await self._run_answer(
                issue_url, issue_number, provider, repo, repo_ctx, issue_ctx, ctx, output, fixer_llm
            )

        # proceed → fix + PR, straight from triage's findings
        return await self._run_fix(
            issue_url, issue_number, provider, repo, repo_ctx, issue_ctx, ctx, output, fixer_llm
        )

    async def _run_answer(
        self,
        issue_url: str,
        issue_number: str,
        provider: str,
        repo: str,
        repo_ctx: RunContext,
        issue_ctx: IssueContext,
        ctx: RunContext,
        triage_output: TriageOutput,
        fixer_llm: FixerLLMChoice,
    ) -> WorkflowResult:
        # No worktree, branch or push gate: nothing here can open a PR.
        data = {
            "kind": "proceed",
            "code_change": False,
            "triage_result": "not_actionable",
            "pr_urls": [],
            "fixer_llm": fixer_llm.report(ctx.model),
        }
        try:
            result = await IssueFixerAgent().invoke(
                IssueFixerInput(
                    issue_url=issue_url,
                    findings=triage_output.findings,
                    code_change=False,
                    issue_title=issue_ctx.issue_title,
                    issue_body=issue_ctx.issue_body,
                    comments=issue_ctx.comments,
                ),
                apply_fixer_llm(repo_ctx, fixer_llm),
            )
        except Exception as exc:
            logger.warning("issue_resolve answer failed for %s: %s", issue_url, exc, exc_info=True)
            return WorkflowResult(
                status="error", summary=f"answer failed: {exc}", data={**data, "error": str(exc)}
            )

        fixer_output = parse_fixer_output(result)
        answer = fixer_output.comment_body if fixer_output else ""
        if not answer:
            return WorkflowResult(
                status="error", summary="fixer agent finished without an answer", data=data
            )
        posted = _post_comment(answer, issue_url, provider, repo, issue_number, ctx)
        if not posted:
            return WorkflowResult(
                status="error", summary="failed to post the answer on the issue", data=data
            )
        return WorkflowResult(status="success", summary="answered on the issue", data=data)

    async def _run_fix(
        self,
        issue_url: str,
        issue_number: str,
        provider: str,
        repo: str,
        repo_ctx: RunContext,
        issue_ctx: IssueContext,
        ctx: RunContext,
        triage_output: TriageOutput,
        fixer_llm: FixerLLMChoice,
    ) -> WorkflowResult:
        branch = issue_branch(issue_number, issue_url)
        platform = provider_to_platform(provider)
        targets = resolve_target_repos(ctx, repo_ctx, triage_output.target_repos)
        multi = len(targets) > 1
        parent_dir = ctx.workspace / "worktrees" / branch.replace("/", "_")

        worktrees: dict[str, WorktreePath] = {}
        prs: dict[str, PRRef] = {}

        def make_open_pr(name: str) -> Callable[[], PRRef]:
            def _open() -> PRRef:
                siblings = [n for n in worktrees if n != name] if multi else []
                pr_ctx = ctx.with_cwd(worktrees[name].path)
                pr = open_pr(
                    branch,
                    pr_title(issue_ctx, name if multi else ""),
                    pr_body(issue_url, issue_ctx, siblings, fixer_llm=fixer_llm.summary),
                    platform,
                    ctx=pr_ctx,
                )
                prs[name] = pr
                pr_ctx.emit(Panel(title="PR Opened", content=f"[bold]{pr.url}[/]", style="green"))
                return pr

            return _open

        try:
            for name, target_ctx in targets:
                dest = (parent_dir / name) if multi else parent_dir
                worktrees[name] = create_worktree(branch, ctx=target_ctx, dest=dest)
                # Force-sync origin to this run's fresh placeholder base *before*
                # the fixer touches anything. A retried execution reuses the same
                # branch name (see issue_branch) but create_worktree always
                # branches fresh off the default branch, so without this a stale
                # commit left on origin by a prior run diverges from what the
                # fixer is about to build on — its own later plain `git push`
                # would then be rejected and it has no prescribed way to recover,
                # burning its turn budget on an unplanned rebase (jc-gitlab-ccdeb1d7).
                # This mirrors sentry_fix's create_worktree -> push_branch pairing.
                push_branch(branch, ctx=ctx.with_cwd(dest))

            fixer_cwd = parent_dir if multi else next(iter(worktrees.values())).path
            fixer_ctx = apply_fixer_llm(ctx.with_cwd(fixer_cwd), fixer_llm)

            stop_hooks = []
            pretool_hooks = []
            for name, _target_ctx in targets:
                wt = worktrees[name]
                wt_ctx = ctx.with_cwd(wt.path)
                stop_hooks.append(require_pushed_fix_hook(wt.path, branch, wt.placeholder_sha))
                # CI verification gates the fixer's StructuredOutput call, not
                # Stop — a Stop block lands after the schema-bound fixer has
                # already finalized its turn and the CLI drops it (see
                # src/agents/hooks.py module docstring / ADR-008).
                pretool_hooks.append(
                    require_pushed_and_ci_pass_hook(
                        wt.path,
                        branch,
                        wt.placeholder_sha,
                        make_open_pr(name),
                        ctx=wt_ctx,
                    )
                )

            fixer_result = await IssueFixerAgent().invoke(
                IssueFixerInput(
                    issue_url=issue_url,
                    branch=branch,
                    findings=triage_output.findings,
                    repos=[
                        {
                            "name": n,
                            "path": str(worktrees[n].path),
                            "ci_bypass_path": str(bypass_marker_path(worktrees[n].path)),
                        }
                        for n, _ in targets
                    ],
                ),
                fixer_ctx,
                extra_hooks={"Stop": stop_hooks, "PreToolUse": pretool_hooks},
            )
            fixer_output = parse_fixer_output(fixer_result)
            if fixer_output and fixer_output.comment_body:
                _post_comment(
                    fixer_output.comment_body,
                    issue_url,
                    provider,
                    repo,
                    issue_number,
                    ctx,
                )

            # Per repo: a real pushed commit always gets its PR opened (or
            # reused) and labeled for review, whether or not CI ended up
            # green — the CI gate hook already gave the fixer its retry budget
            # to fix anything it broke (see require_pushed_and_ci_pass_hook);
            # a failure that survives that isn't grounds to leave the work
            # sitting unreviewed, and it isn't a run failure either — "error"
            # here means the run itself broke (nothing pushed, an exception),
            # not "the generated fix didn't pass CI." ci_result is recorded
            # per repo for visibility only, it doesn't gate anything.
            repo_results: dict[str, dict[str, str]] = {}
            for name, _target_ctx in targets:
                wt = worktrees[name]
                wt_ctx = ctx.with_cwd(wt.path)
                missing = verify_fix_pushed(branch, wt.placeholder_sha, ctx=wt_ctx)
                if missing:
                    # Nothing pushed here — expected when this target repo
                    # turned out not to need a change. Not an error on its
                    # own; only "no repo got a real fix" is.
                    continue
                pr = prs.get(name) or make_open_pr(name)()
                ci_result = await asyncio.to_thread(check_ci, pr, cwd=wt.path)
                attach_label(pr, REVIEW_LABEL, ctx=wt_ctx)
                attach_label(pr, SUMMARY_LABEL, ctx=wt_ctx)
                repo_results[name] = {"pr_url": pr.url, "ci": ci_result.outcome}

            pr_urls = [r["pr_url"] for r in repo_results.values()]

            if not repo_results:
                return WorkflowResult(
                    status="error",
                    summary="fixer agent finished without pushing a real change to any repo",
                    data={
                        "kind": "proceed",
                        "triage_result": "actionable",
                        "pr_urls": [],
                        "fixer_llm": fixer_llm.report(ctx.model),
                    },
                )

            return WorkflowResult(
                status="success",
                summary=f"PR{'s' if len(pr_urls) > 1 else ''} opened: {', '.join(pr_urls)}",
                data={
                    "kind": "proceed",
                    "triage_result": "actionable",
                    "pr_urls": pr_urls,
                    "repos": repo_results,
                    "fixer_llm": fixer_llm.report(ctx.model),
                },
            )
        except Exception as exc:
            logger.warning(
                "issue_resolve fix path failed for %s: %s", issue_url, exc, exc_info=True
            )
            return WorkflowResult(
                status="error",
                summary=f"fix failed: {exc}",
                data={
                    "kind": "proceed",
                    "triage_result": "actionable",
                    "pr_urls": [pr.url for pr in prs.values()],
                    "fixer_llm": fixer_llm.report(ctx.model),
                    "error": str(exc),
                },
            )


def _post_comment(
    body: str, issue_url: str, provider: str, repo: str, issue_number: str, ctx: RunContext
) -> bool:
    try:
        post_issue_comment(body, provider, repo, issue_number, ctx=ctx)
    except Exception:
        logger.warning("Failed to post comment on issue %s", issue_url, exc_info=True)
        return False
    return True
