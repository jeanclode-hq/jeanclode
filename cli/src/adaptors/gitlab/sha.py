"""Resolve MR head metadata (SHA + source branch) via `glab mr view`."""

from __future__ import annotations

import json
import logging
import os
import subprocess

from src.adaptors.gitlab.client import parse_mr_url

logger = logging.getLogger(__name__)


def _fetch_mr(url: str, token: str) -> dict | None:
    """Return `glab mr view -F json` for an MR URL, or None on failure.

    Failures are logged at warning level with glab's own stderr: in the
    sandbox the only credential is whatever the security-proxy injects, so a
    401 here surfaces as unparseable stdout and would otherwise be
    indistinguishable from "this URL isn't an MR" — the run then silently
    degrades to the default branch and only fails later, at clone time.
    """
    parsed = parse_mr_url(url)
    if not parsed:
        return None
    host, project, iid = parsed
    env = {**os.environ, "GITLAB_TOKEN": token}
    if host != "gitlab.com":
        env["GITLAB_HOST"] = f"https://{host}"
    try:
        proc = subprocess.run(
            ["glab", "mr", "view", str(iid), "-R", project, "-F", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("glab mr view failed for %s", url, exc_info=True)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "glab mr view returned no JSON for %s (exit=%s): %s",
            url,
            proc.returncode,
            (proc.stderr or proc.stdout).strip()[:500] or "<no output>",
        )
        return None


def fetch_mr_head_sha(url: str, token: str) -> str | None:
    """Return the head commit SHA of a GitLab MR, or None on failure."""
    data = _fetch_mr(url, token)
    if data is None:
        return None
    sha = data.get("sha") or data.get("diff_refs", {}).get("head_sha")
    return sha or None


def fetch_mr_head_ref(url: str, token: str) -> str | None:
    """Return the source branch name of a GitLab MR.

    Used by the workspace fetcher when the target skill needs a writable
    git tree — see :func:`fetch_pr_head_ref` for the GitHub mirror.
    """
    data = _fetch_mr(url, token)
    if data is None:
        return None
    return data.get("source_branch") or None
