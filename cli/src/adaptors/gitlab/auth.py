"""GitLab authentication — resolve token from env or `glab`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def resolve_token(host: str | None = None) -> str | None:
    """Resolve a GitLab token.

    Env wins — ``GITLAB_TOKEN``, then ``GITLAB_ACCESS_TOKEN`` (the name
    ``glab`` itself honours). Otherwise ask ``glab`` for whatever it is
    configured with (env, config file, or keyring) for ``host`` or its
    default instance.
    """
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_ACCESS_TOKEN")
    if token:
        return token

    if not shutil.which("glab"):
        return None

    # `glab auth token` does not exist on current glab; `auth status
    # --show-token` is the portable way to read the resolved token.
    cmd = ["glab", "auth", "status", "--show-token"]
    if host:
        cmd += ["--hostname", host]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except subprocess.SubprocessError, OSError:
        logger.debug("Failed to invoke `glab auth status`", exc_info=True)
        return None

    for line in (result.stdout + result.stderr).splitlines():
        # e.g. "  ✓ Token found: glpat-xxxx" (older glab: "Token: glpat-xxxx")
        _, sep, rest = line.partition("Token found:")
        if not sep:
            _, sep, rest = line.partition("Token:")
        candidate = rest.strip()
        if sep and candidate and "*" not in candidate:
            logger.debug("Using token from `glab auth status`")
            return candidate

    return None
