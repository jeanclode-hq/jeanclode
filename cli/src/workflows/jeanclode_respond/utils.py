"""Helpers shared by the jeanclode-respond workflow."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, cast

from src.activities.git.schemas import PRRef
from src.activities.respond.schemas import MentionContext
from src.agents.respond.schemas import PlannerOutput
from src.agents.schemas import AgentResult

logger = logging.getLogger(__name__)


def _read_file(cache: Path, name: str, *, strip: bool = True) -> str:
    path = cache / name
    if not path.is_file():
        return ""
    text = path.read_text()
    return text.strip() if strip else text


def load_mention_context(repo_dir: Path) -> MentionContext | None:
    """Load mention metadata the adaptor preflight wrote under .context/."""
    cache = repo_dir / ".context"
    if not cache.is_dir():
        return None

    platform = _read_file(cache, "platform")
    repo = _read_file(cache, "repo")
    if platform not in ("github", "gitlab") or not repo:
        return None
    return MentionContext(
        platform=cast("Any", platform),
        repo=repo,
        pr=_read_file(cache, "pr"),
        issue=_read_file(cache, "issue"),
        surface=_read_file(cache, "surface") or "unknown",
        target_url=_read_file(cache, "target_url"),
        mention_body=_read_file(cache, "mention_body", strip=False),
        mention_author=_read_file(cache, "mention_author") or "unknown",
        thread_id=_read_file(cache, "thread_id"),
        comment_id=_read_file(cache, "comment_id"),
        pr_author=_read_file(cache, "pr_author"),
    )


def load_pr_snapshot(repo_dir: Path) -> tuple[str, str, str]:
    """Load ``(pr_description, diff, discussions)`` from ``.context/``.

    Empty strings when the cache or any field is missing — issue
    mentions (no PR) and bare-URL runs both legitimately have none.
    """
    cache = repo_dir / ".context"
    if not cache.is_dir():
        return "", "", ""
    return (
        _read_file(cache, "pr_description", strip=False),
        _read_file(cache, "diff", strip=False),
        _read_file(cache, "discussions", strip=False),
    )


def parse_planner_output(result: AgentResult) -> PlannerOutput | None:
    if result.structured is not None:
        try:
            return PlannerOutput.model_validate(result.structured)
        except Exception as exc:
            logger.warning("planner returned invalid PlannerOutput: %s", exc)
    return None


def read_current_sha(cwd: Path) -> str:
    """Return the local ``HEAD`` commit SHA, or ``""`` on failure.

    Captured before the planner runs — the deterministic baseline the
    post-turn push check compares against. See ``branch_was_pushed``.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def read_current_branch(cwd: Path) -> str:
    """Return the checked-out branch name, or ``""`` when detached or unreadable."""
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    branch = proc.stdout.strip()
    return "" if branch == "HEAD" else branch


def pr_ref_from_mention(mention: MentionContext, branch: str) -> PRRef | None:
    """Build the ``PRRef`` ci-watch needs from a PR/MR mention.

    ``target_url`` is the *comment* URL the adaptor was triggered on, so the
    PR/MR url is rebuilt from its origin — ``pr_id`` reads the trailing
    segment to address the MR/PR whose pipeline it polls.
    """
    if not mention.pr or not branch:
        return None
    origin = "/".join(mention.target_url.split("/")[:3])
    if not origin.startswith("http"):
        return None
    path = "pull" if mention.platform == "github" else "-/merge_requests"
    return PRRef(
        url=f"{origin}/{mention.repo}/{path}/{mention.pr}",
        branch=branch,
        platform=mention.platform,
    )


def branch_was_pushed(cwd: Path, starting_sha: str) -> bool:
    """True iff origin's current branch has moved past ``starting_sha``.

    Judgment-free: never trusts what the planner reported, never pushes
    anything itself. Runs unconditionally after every planner turn —
    whether and when to push was entirely the planner's call.
    """
    if not starting_sha:
        return False
    branch = read_current_branch(cwd)
    if not branch:
        return False
    ls_proc = subprocess.run(
        ["git", "ls-remote", "origin", branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if ls_proc.returncode != 0 or not ls_proc.stdout.strip():
        return False
    tip = ls_proc.stdout.split()[0]
    return tip != starting_sha
