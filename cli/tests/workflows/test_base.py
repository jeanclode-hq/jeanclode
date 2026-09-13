"""Tests for the workflow registry helpers."""

from __future__ import annotations

from src.workflows.base import command_names


def test_command_names_includes_workflow_only_commands() -> None:
    """ "echo" has no adaptor behind it (see src/adaptors/*/adaptor.py) — only
    a bare ``command:echo`` trigger — so cli.py's known-commands check needs
    this to recognize it as a valid subcommand.
    """
    assert "echo" in command_names()
