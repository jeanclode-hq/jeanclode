"""Small helpers for the sentry-fix workflow.

PR title/body shaping, branch naming, platform detection, structured-output
parsing, triage-panel rendering, and the per-group fallback used when the
synthesis agent returns nothing. Kept out of ``runner.py`` so that file
stays focused on orchestration.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import Literal

from pydantic import ValidationError

from src.activities.sentry import (
    RoutingPlan,
    SynthesisGroup,
    TriagedIssue,
    TriageOutput,
    group_from_issues,
)
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def detect_platform(ctx: RunContext) -> Literal["github", "gitlab"]:
    """Decide github vs gitlab by reading the cloned repo's origin URL."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        return "github"
    return "gitlab" if "gitlab" in proc.stdout else "github"


def branch_name(group: SynthesisGroup) -> str:
    """Deterministic per-group branch name.

    Hashed on issue_ids (stable) rather than root_cause (LLM freeform text
    that can reword between synthesis runs) and with no timestamp suffix,
    so a retried execution lands back on the same branch/PR instead
    of opening an orphaned duplicate — see push_branch's force-push and
    open_pr's reuse.

    The leading number is derived from the same digest (not randomly
    chosen), so it stays reproducible across retries with no state to
    persist. Sentry issues have no natural small issue number, but
    test-deploy tooling often needs a `fix/{number}-...` branch to key
    off — this is best-effort (no live uniqueness check against other
    branches): a 10000-wide slot space is fine given how rarely sentry-fix
    branches get deployed for testing.
    """
    digest = hashlib.sha1("+".join(sorted(group.issue_ids)).encode()).hexdigest()
    number = 10000 + (int(digest[:8], 16) % 10000)
    return f"fix/{number}-sentry-{digest[:7]}"


def pr_title(group: SynthesisGroup, repo_name: str = "") -> str:
    head = group.root_cause.split("\n", 1)[0]
    cleaned = head.replace("`", "").replace('"', "").replace("'", "")[:68]
    suffix = f" ({repo_name})" if repo_name else ""
    return f"fix: {cleaned}{suffix}"


def pr_body(
    group: SynthesisGroup, siblings: list[str] | None = None, *, fixer_llm: str = ""
) -> str:
    """PR/MR body. ``siblings`` names the OTHER repos this same fix touches
    (multi-repo case) — each repo gets its own PR, this just cross-links
    them by name so a reviewer on one isn't confused why it's incomplete on
    its own."""
    urls = "\n".join(f"- {u}" for u in group.sentry_urls) or "- (none)"
    files = ", ".join(group.affected_files) if group.affected_files else "unknown"
    note = (
        "\n\n_Part of a multi-repo fix — also touches: "
        + ", ".join(f"`{s}`" for s in siblings)
        + "._"
        if siblings
        else ""
    )
    return (
        f"## Sentry Issues\n\n{urls}\n\n"
        f"## Root Cause\n\n{group.root_cause}{note}\n\n"
        f"## Triage\n\n"
        f"- **Category:** {group.category or 'bug'}\n"
        f"- **Confidence:** {group.confidence}\n"
        f"- **Files:** {files}\n" + (f"- **Fixer LLM:** {fixer_llm}\n" if fixer_llm else "") + "\n"
        "---\n*Opened by Jeanclode. Code changes incoming.*"
    )


_TRIAGE_FIELDS = frozenset(TriageOutput.model_fields)


def triage_from_result(structured: dict[str, object] | None) -> TriageOutput | None:
    """Parse the triage agent's structured output, or None if it gave none.

    None is not the same as "not actionable". A missing payload, or one
    whose keys the schema doesn't recognise, validates to an all-defaults
    TriageOutput — `kind="not_actionable"`, empty reason — which is
    indistinguishable from a real verdict and silently drops an issue the
    agent may well have investigated. The caller reports these as a run
    failure instead of a clean skip.
    """
    if not structured:
        return None
    if not _TRIAGE_FIELDS & {str(k) for k in structured}:
        logger.warning(
            "triage structured output matched no known field (keys: %s)",
            ", ".join(sorted(str(k) for k in structured)),
        )
        return None
    try:
        return TriageOutput.model_validate(structured)
    except ValidationError:
        logger.warning("triage structured output failed validation", exc_info=True)
        return None


def format_triage_panel(plan: RoutingPlan) -> str:
    """Render the triage table that the legacy CLI displayed after routing.

    Each issue gets a status chip (its triage ``kind``, or the reason the
    filter dropped it) plus its confidence, category, and root-cause
    hypothesis. The trailing line summarizes the routing decision.
    """
    passed: set[str] = set()
    for grp in plan.groups:
        passed.update(grp.issue_ids)
    for item in plan.actionable:
        passed.add(item.issue_id)

    lines: list[str] = []
    for issue_id, record in plan.triages.items():
        triage = record.triage
        status = _triage_status(issue_id, record, passed)
        lines.append(f"[bold]Issue {issue_id}[/] — {status}")
        if record.sentry_url:
            lines.append(f"  [dim]{record.sentry_url}[/]")
        lines.append(f"  confidence: {triage.confidence:.0%}  category: {triage.category}")
        if triage.root_cause_hypothesis:
            lines.append(f"  root cause: {triage.root_cause_hypothesis}")
        if record.target_repos:
            lines.append(f"  repos: {', '.join(record.target_repos)}")
        if triage.kind != "proceed" and triage.reason:
            lines.append(f"  reason: {triage.reason}")
        lines.append("")

    if plan.unparsed:
        lines.append(
            f"[red]Triage returned no usable output for issue(s) "
            f"{', '.join(plan.unparsed)} — investigated, but the verdict was "
            f"lost on the way back. Not counted as 'not actionable'.[/]"
        )

    if plan.action == "stop" and plan.unresolved_repo:
        dropped = ", ".join(t.issue_id for t in plan.unresolved_repo)
        lines.append(
            f"[red]Stopping: issue(s) {dropped} are actionable but name no repo "
            f"checked out for this run.[/]"
        )
    elif plan.action == "stop":
        lines.append("[yellow]No actionable issues after filtering — stopping.[/]")
    elif plan.action == "synthesize":
        lines.append(f"[cyan]{len(plan.actionable)} actionable issues → synthesizing groups[/]")
    elif plan.action == "fix":
        lines.append(f"[cyan]{len(plan.groups)} group(s) → fixing[/]")
    return "\n".join(lines).rstrip()


_KIND_LABEL: dict[str, str] = {
    "not_actionable": "[dim]not actionable[/]",
    "duplicate": "[yellow]duplicate[/]",
    "already_fixed": "[yellow]already fixed[/]",
    "previously_rejected": "[yellow]previously rejected[/]",
}


def _triage_status(issue_id: str, record: TriagedIssue, passed_filter: set[str]) -> str:
    triage = record.triage
    if issue_id in passed_filter:
        return "[green]actionable[/]"
    label = _KIND_LABEL.get(triage.kind)
    if label:
        return label
    if triage.existing_pr_url:
        return f"[yellow]existing PR[/] [dim]{triage.existing_pr_url}[/]"
    if not record.target_repos:
        return "[red]no target repo checked out[/]"
    return "[dim]filtered[/]"


def serialize_triages(plan: RoutingPlan) -> dict[str, object]:
    return {k: v.model_dump(mode="json") for k, v in plan.triages.items()}


def fallback_groups(actionable: list[TriagedIssue]) -> list[SynthesisGroup]:
    """If the synthesis agent returned nothing usable, treat each issue as its own group."""
    return [group_from_issues([t]) for t in actionable]


def synthesized_groups(
    parsed_groups: list[SynthesisGroup], actionable: list[TriagedIssue]
) -> list[SynthesisGroup]:
    """Rebuild each synthesis group from the triaged issues it names.

    The agent's only real decision is the partition — which issue_ids
    belong together. Every other field (target repos, findings, files,
    confidence) is rebuilt here from the triage records so a reworded or
    hallucinated repo name in its JSON can never reach the runner, which
    opens a PR against whatever those names resolve to. Unknown ids are
    dropped; issues the agent forgot become their own group so nothing is
    silently lost.
    """
    by_id = {t.issue_id: t for t in actionable}
    groups: list[SynthesisGroup] = []
    claimed: set[str] = set()
    for grp in parsed_groups:
        members = [by_id[i] for i in grp.issue_ids if i in by_id and i not in claimed]
        if not members:
            continue
        claimed.update(m.issue_id for m in members)
        rebuilt = group_from_issues(members)
        # The agent's prose summary of *why* these belong together is the
        # one thing it does own — keep it when it wrote one.
        if grp.root_cause:
            rebuilt.root_cause = grp.root_cause
        groups.append(rebuilt)
    missed = [t for t in actionable if t.issue_id not in claimed]
    if missed:
        logger.warning(
            "synthesis agent omitted %d issue(s): %s; each becomes its own group",
            len(missed),
            ", ".join(t.issue_id for t in missed),
        )
        groups.extend(group_from_issues([t]) for t in missed)
    return groups
