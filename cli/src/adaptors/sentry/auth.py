"""Sentry authentication — resolve auth token from env or sentryclirc."""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_token() -> str | None:
    """Resolve Sentry auth token: SENTRY_AUTH_TOKEN env var → ~/.sentryclirc fallback."""
    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if token:
        return token

    rc_path = Path.home() / ".sentryclirc"
    if rc_path.is_file():
        try:
            config = configparser.ConfigParser()
            config.read(rc_path)
            token = config.get("auth", "token", fallback=None)
            if token:
                logger.debug("Using token from %s", rc_path)
                return token
        except Exception:
            logger.debug("Failed to read %s", rc_path, exc_info=True)

    return None
