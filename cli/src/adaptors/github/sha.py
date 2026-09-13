"""Resolve PR head metadata (SHA + branch ref) via `gh pr view`."""

from __future__ import annotations

import logging
import os
import subprocess

from src.adaptors.github.client import parse_pr_url

logger = logging.getLogger(__name__)


def fetch_pr_head_sha(url: str, token: str) -> str | None:
    """Return the head commit SHA of a GitHub PR, or None on failure."""
    parsed = parse_pr_url(url)
    if not parsed:
        return None
    _owner, _repo, number = parsed
    owner_repo = "/".join(parsed[:2])
    env = {**os.environ, "GH_TOKEN": token}
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "-R",
                owner_repo,
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("gh pr view failed for %s", url, exc_info=True)
        return None
    sha = proc.stdout.strip()
    if not sha:
        logger.warning(
            "gh pr view returned no headRefOid for %s (exit=%s): %s",
            url,
            proc.returncode,
            (proc.stderr or "").strip()[:500] or "<no output>",
        )
    return sha or None


def fetch_pr_head_ref(url: str, token: str) -> str | None:
    """Return the head branch name (``headRefName``) of a GitHub PR.

    Used by the workspace fetcher when the target skill needs a writable
    git tree (respond) — the ref name is what ``git clone --branch`` and
    ``git push origin HEAD`` need to operate on a real local branch.
    """
    parsed = parse_pr_url(url)
    if not parsed:
        return None
    _owner, _repo, number = parsed
    owner_repo = "/".join(parsed[:2])
    env = {**os.environ, "GH_TOKEN": token}
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "-R",
                owner_repo,
                "--json",
                "headRefName",
                "-q",
                ".headRefName",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("gh pr view --json headRefName failed for %s", url, exc_info=True)
        return None
    ref = proc.stdout.strip()
    if not ref:
        logger.warning(
            "gh pr view returned no headRefName for %s (exit=%s): %s",
            url,
            proc.returncode,
            (proc.stderr or "").strip()[:500] or "<no output>",
        )
    return ref or None
