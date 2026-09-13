"""GitHub authentication — resolve token from env or `gh auth token`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def resolve_token() -> str | None:
    """Resolve GitHub token: GITHUB_TOKEN / GH_TOKEN env vars → `gh auth token` fallback."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(var)
        if token:
            return token

    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            token = result.stdout.strip()
            if token:
                logger.debug("Using token from `gh auth token`")
                return token
        except (subprocess.SubprocessError, OSError):
            logger.debug("Failed to invoke `gh auth token`", exc_info=True)

    return None
