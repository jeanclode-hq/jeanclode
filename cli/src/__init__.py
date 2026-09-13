"""Jeanclode CLI — triage Sentry errors and invoke Claude Code to fix bugs."""

import importlib.metadata

try:
    __version__: str = importlib.metadata.version("jeanclode")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0-dev"
