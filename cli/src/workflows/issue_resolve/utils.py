"""Helpers for the issue-resolve workflow."""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

from pydantic import BaseModel

from src.activities.issue.schemas import IssueContext
from src.agents.issue.schemas import IssueFixerOutput, TriageOutput
from src.agents.schemas import AgentResult

logger = logging.getLogger(__name__)

_KIND_COLOR: dict[str, str] = {
    "proceed": "green",
    "needs_info": "yellow",
    "duplicate": "yellow",
    "already_fixed": "yellow",
    "split": "yellow",
    "push_back": "red",
    "refuse": "red",
}


def format_triage_panel(output: TriageOutput, issue_url: str = "") -> str:
    """Render the triage result as Rich markup for the Panel display."""
    color = _KIND_COLOR.get(output.kind, "white")
    lines: list[str] = []
    if issue_url:
        lines.append(f"[dim]{issue_url}[/]")
    lines.append(f"outcome: [{color}]{output.kind}[/]")
    if output.kind == "proceed" and not output.code_change:
        lines.append("deliverable: [bold]answer on the issue[/] (no code change)")
    if output.target_repos:
        lines.append(f"target repos: [bold]{', '.join(output.target_repos)}[/]")
    if output.reasoning:
        lines.append(f"\n{output.reasoning}")
    if output.comment_body and output.kind != "proceed":
        lines.append("\n[dim]→ comment will be posted on the issue[/]")
    return "\n".join(lines)


def repo_url_from_parts(provider: str, repo: str) -> str:
    """Build a clone URL from provider and repo slug.

    GitHub repo is ``owner/name``; GitLab repo is ``host/project/path``.
    """
    if provider == "github":
        return f"https://github.com/{repo}"
    return f"https://{repo}"


def parse_issue_url_info(issue_url: str) -> tuple[str, str, str]:
    """Return ``(provider, repo, issue_number)`` parsed from a GitHub or GitLab issue URL.

    ``repo`` is ``"owner/repo"`` for GitHub and ``"host/project/path"`` for GitLab.
    Raises ``ValueError`` if the URL is not a recognised issue URL.
    """
    # GitLab work_items URLs are equivalent to issues URLs — normalise early so
    # all downstream code (glab CLI, PR descriptions, comments) uses the stable path.
    issue_url = issue_url.replace("/-/work_items/", "/-/issues/")

    from src.adaptors.github.client import parse_issue_url as parse_gh
    from src.adaptors.gitlab.client import parse_issue_url as parse_gl

    parsed_gh = parse_gh(issue_url)
    if parsed_gh is not None:
        owner, repo, number = parsed_gh
        return "github", f"{owner}/{repo}", str(number)

    parsed_gl = parse_gl(issue_url)
    if parsed_gl is not None:
        host, project, iid = parsed_gl
        return "gitlab", f"{host}/{project}", str(iid)

    msg = f"Could not parse issue URL: {issue_url}"
    raise ValueError(msg)


def provider_to_platform(provider: str) -> Literal["github", "gitlab"]:
    """Map provider string to the platform literal used by PR activities."""
    if provider == "gitlab":
        return "gitlab"
    return "github"


def issue_branch(issue_number: str, issue_url: str) -> str:
    """Deterministic per-issue branch name.

    Issue number leads (matches the `fix/{number}-...` convention test-deploy
    tooling commonly parses). No timestamp suffix: a retried execution
    must land back on the same branch (and thus the same draft PR) rather
    than opening an orphaned duplicate — see push_branch's force-push and
    open_pr's reuse.
    """
    digest = hashlib.sha1(issue_url.encode()).hexdigest()[:7]
    return f"fix/{issue_number}-issue-{digest}"


def pr_title(issue_ctx: IssueContext, repo_name: str = "") -> str:
    head = (issue_ctx.issue_title or "resolve issue").split("\n", 1)[0]
    cleaned = head.replace("`", "").replace('"', "").replace("'", "")[:68]
    suffix = f" ({repo_name})" if repo_name else ""
    return f"fix: {cleaned}{suffix}"


def pr_body(
    issue_url: str,
    issue_ctx: IssueContext,
    siblings: list[str] | None = None,
    *,
    fixer_llm: str = "",
) -> str:
    """PR/MR body. ``siblings`` names the OTHER repos also part of this fix
    (multi-repo case) — each repo gets its own PR, this just cross-links
    them by name so a reviewer on one isn't confused why it's incomplete
    on its own."""
    title = issue_ctx.issue_title or "(no title)"
    note = (
        "\n\n_Part of a multi-repo fix for this issue — also touches: "
        + ", ".join(f"`{s}`" for s in siblings)
        + "._"
        if siblings
        else ""
    )
    if fixer_llm:
        note += f"\n\n_Fixer LLM: {fixer_llm}_"
    return (
        f"Resolves {issue_url}\n\n"
        f"## Issue\n\n**{title}**{note}\n\n"
        f"---\n*Opened by Jeanclode after the fix was pushed.*"
    )


def parse_triage_output(result: AgentResult) -> TriageOutput | None:
    """Extract a validated ``TriageOutput`` from an ``AgentResult``."""
    return _parse_output(result, TriageOutput)


def parse_fixer_output(result: AgentResult) -> IssueFixerOutput | None:
    """Extract a validated ``IssueFixerOutput`` from an ``AgentResult``."""
    return _parse_output(result, IssueFixerOutput)


def _parse_output[T: BaseModel](result: AgentResult, model: type[T]) -> T | None:
    if result.structured:
        try:
            return model.model_validate(result.structured)
        except Exception:
            logger.warning("Failed to parse %s structured output", model.__name__, exc_info=True)
    # Fallback: try parsing text as JSON
    if result.text:
        from src.agents.utils import try_parse_json_object

        parsed = try_parse_json_object(result.text)
        if parsed:
            try:
                return model.model_validate(parsed)
            except Exception:
                logger.warning(
                    "Failed to parse %s text output as JSON", model.__name__, exc_info=True
                )
    return None
