"""Resolve triage's ``target_repos`` names to the checkouts on disk.

Shared by issue-resolve and sentry-fix: both let their triage agent name
which repo(s) a fix belongs in, choosing among the primary checkout and
the ``--related-repo`` siblings cloned alongside it (``ctx.related_repos``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def resolve_target_repos(
    ctx: RunContext,
    primary_ctx: RunContext,
    target_repos: list[str],
    *,
    allow_primary_fallback: bool = True,
) -> list[tuple[str, RunContext]]:
    """Return ``[(name, RunContext), ...]`` for every repo the fix should
    get a worktree in.

    Triage's ``target_repos`` is the complete, authoritative list of repo
    names that need a code change — the primary is not force-included.
    This matters when the primary is a thin/empty wrapper repo (e.g. a QA
    scaffold) and the real fix lives entirely in a related repo:
    force-including the primary broke the whole run when it had no commits
    yet to branch from (see jc-gitlab-c4967424).

    A name that doesn't match the primary or any cloned related repo is
    dropped with a warning rather than failing the run. If nothing
    resolves (empty list, or every name unknown) we fall back to the
    primary alone — a run should never end up with zero targets.

    ``allow_primary_fallback=False`` turns both of those primary-side
    behaviours off: the primary is a candidate only when it really is a
    git checkout, and unknown names resolve to nothing rather than to the
    primary. Sentry-fix needs that — its cwd is a bare workspace when no
    repo mapping resolved, and "triage named a repo we don't have here"
    must stop the run, not silently retarget the fix at whatever repo
    happened to be cloned (jc-sentry-1887773).
    """
    primary_name = primary_ctx.cwd.name
    include_primary = allow_primary_fallback or (primary_ctx.cwd / ".git").exists()
    available: dict[str, RunContext] = {
        r["name"]: ctx.with_cwd(Path(r["path"])) for r in ctx.related_repos
    }
    if include_primary:
        available[primary_name] = primary_ctx  # primary wins any name collision

    resolved: list[tuple[str, RunContext]] = []
    seen: set[str] = set()
    for name in target_repos:
        if name in seen:
            continue
        target_ctx = available.get(name)
        if target_ctx is None:
            logger.warning(
                "triage target_repos entry %r not found among primary/related repos %s; skipping",
                name,
                list(available),
            )
            continue
        resolved.append((name, target_ctx))
        seen.add(name)

    if not resolved and allow_primary_fallback:
        logger.warning(
            "resolve_target_repos: nothing resolved from %s; falling back to primary %r",
            target_repos,
            primary_name,
        )
        resolved = [(primary_name, primary_ctx)]
    return resolved
