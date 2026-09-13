"""Filter triage results into a deterministic routing plan."""

from __future__ import annotations

from src.activities.decorator import activity
from src.activities.git.targets import resolve_target_repos
from src.activities.sentry.schemas import (
    RoutingPlan,
    SynthesisGroup,
    TriagedIssue,
)
from src.runtime.context import RunContext


def group_from_issues(issues: list[TriagedIssue]) -> SynthesisGroup:
    """Build a group from the issues it contains.

    Every field is derived from the members, including ``target_repos``
    and ``findings``: which repos a fix touches, and the investigation
    notes behind it, are triage's answers — the synthesis agent only
    decides *which issues belong together*, and must never be the source
    of truth for a repo name the runner is about to open a PR against.
    """
    repos: list[str] = []
    files: list[str] = []
    for issue in issues:
        for name in issue.target_repos:
            if name not in repos:
                repos.append(name)
        for path in issue.triage.affected_files:
            if path not in files:
                files.append(path)
    findings = "\n\n".join(
        f"### Sentry issue {i.issue_id} ({i.sentry_url})\n\n{i.triage.findings}"
        for i in issues
        if i.triage.findings
    )
    first = issues[0]
    return SynthesisGroup(
        issue_ids=[i.issue_id for i in issues],
        root_cause=first.triage.root_cause_hypothesis or "Unknown",
        target_repos=repos,
        sentry_urls=[i.sentry_url for i in issues],
        affected_files=files,
        category=first.triage.category,
        confidence=min(i.triage.confidence for i in issues),
        findings=findings,
    )


@activity(name="Filtering and routing")
def filter_and_route(
    triages: list[TriagedIssue], *, ctx: RunContext, unparsed: list[str] | None = None
) -> RoutingPlan:
    """Apply the deterministic filter and pick stop / fix / synthesize.

    An issue must be "proceed", carry no pending PR/MR, and name at least
    one repo that's actually checked out for this run. Single survivor →
    fix; multiple → synthesize first; none → stop.

    Repo resolution is by name against the checkouts on disk (the primary
    plus ``ctx.related_repos``), not by a URL the triage agent or Sentry's
    code-mappings API happened to supply. A Sentry issue URL carries no
    repo identity, and self-hosted Sentry has no usable code mapping, so
    those sources are null far more often than not — dropping an
    otherwise-actionable issue over them is how a correct triage ends up
    fixing nothing (jc-sentry-1887773).
    """
    indexed: dict[str, TriagedIssue] = {}
    actionable: list[TriagedIssue] = []
    unresolved: list[TriagedIssue] = []
    unparsed = list(unparsed or [])

    primary = [ctx.cwd.name] if (ctx.cwd / ".git").exists() else []
    for issue in triages:
        # An empty target_repos means triage didn't name one, not that it
        # ruled the checkout out — use the repo this run was dispatched
        # against. Names it *did* give never fall back: a name we can't
        # resolve means the fix belongs somewhere we haven't cloned, and
        # retargeting it at the primary would open a PR on the wrong repo.
        requested = issue.triage.target_repos or primary
        names = [
            name
            for name, _target_ctx in resolve_target_repos(
                ctx,
                ctx,
                requested,
                allow_primary_fallback=False,
            )
        ]
        record = TriagedIssue(
            issue_id=issue.issue_id,
            sentry_url=issue.sentry_url,
            triage=issue.triage,
            target_repos=names,
        )
        indexed[issue.issue_id] = record

        if issue.triage.kind != "proceed":
            continue
        if issue.triage.existing_pr_url:
            continue
        if not names:
            unresolved.append(record)
            continue

        actionable.append(record)

    if not actionable:
        if unresolved:
            reason = "Actionable issues dropped — no target repo is checked out for this run"
        elif unparsed:
            reason = f"Triage returned no usable output for {len(unparsed)} issue(s)"
        else:
            reason = "No actionable issues after filtering"
        return RoutingPlan(
            action="stop",
            reason=reason,
            triages=indexed,
            unresolved_repo=unresolved,
            unparsed=unparsed,
        )

    if len(actionable) == 1:
        return RoutingPlan(
            action="fix",
            groups=[group_from_issues(actionable)],
            triages=indexed,
            unresolved_repo=unresolved,
            unparsed=unparsed,
        )

    return RoutingPlan(
        action="synthesize",
        actionable=actionable,
        triages=indexed,
        unresolved_repo=unresolved,
        unparsed=unparsed,
    )
