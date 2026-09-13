"""Adaptor registry — auto-discovers and matches adaptors to URLs."""

from __future__ import annotations

from src.adaptors.base import Adaptor
from src.adaptors.github.adaptor import GithubAdaptor
from src.adaptors.gitlab.adaptor import GitlabAdaptor
from src.adaptors.sentry.adaptor import SentryAdaptor

# Register all known adaptors. Add new ones here.
_ADAPTORS: list[Adaptor] = [
    SentryAdaptor(),
    GithubAdaptor(),
    GitlabAdaptor(),
]


def find_adaptor(url: str) -> Adaptor | None:
    """Find the adaptor that handles a given URL. Returns None if no match."""
    for adaptor in _ADAPTORS:
        if adaptor.matches(url):
            return adaptor
    return None


def list_commands() -> list[str]:
    """Return all registered subcommand names across adaptors."""
    cmds: set[str] = set()
    for adaptor in _ADAPTORS:
        cmds.update(adaptor.skills.keys())
    return sorted(cmds)
