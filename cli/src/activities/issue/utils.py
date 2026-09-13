"""Shared helpers for issue activities."""

from __future__ import annotations

import os

from src.runtime.context import RunContext


def _run_env(ctx: RunContext) -> dict[str, str]:
    """Merge ctx.env into os.environ so gh/glab inherit the forge token."""
    return {**os.environ, **ctx.env}
