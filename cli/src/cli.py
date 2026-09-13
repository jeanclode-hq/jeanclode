"""Argument parsing for the jeanclode CLI.

Supports two invocation styles:
    jeanclode sentry https://sentry.io/issues/123       # explicit command
    jeanclode https://sentry.io/issues/123               # auto-detect from URL
    jeanclode sentry url1 url2 --repo org/api            # multiple URLs with --repo
"""

from __future__ import annotations

import sys

from pydantic import BaseModel, ConfigDict


class URLInput(BaseModel):
    """A single URL with optional repo binding."""

    model_config = ConfigDict(frozen=True)

    url: str
    repo: str | None = None
    related_repos: tuple[str, ...] = ()


class CLIArgs(BaseModel):
    """Parsed CLI arguments."""

    model_config = ConfigDict(frozen=True)

    command: str  # "run", "config", or a specific adaptor command like "sentry"
    adaptor_command: str | None = None  # explicit command name if given (e.g. "sentry")
    urls: tuple[URLInput, ...] = ()
    dry_run: bool = False
    debug: bool = False
    model: str | None = None

    @property
    def issues(self) -> tuple[URLInput, ...]:
        """Alias for backward compat."""
        return self.urls


_VALID_MODELS = {"haiku", "sonnet", "opus"}


def _help_text(commands: list[str]) -> str:
    cmds = ", ".join(commands) if commands else "sentry"
    return f"""\
usage: jeanclode [command] URL [URL ...] [--repo REPO] [--related-repo REPO] [--dry-run] [--debug] [--model MODEL]

Auto-detect what to do from the URL, or specify a command explicitly.

commands:
  {cmds}
  config                Run interactive setup

positional arguments:
  URL                   Issue/PR/repo URL(s)

options:
  -h, --help            show this help message and exit
  --repo REPO           Repository URL — applies to all preceding URLs
  --related-repo REPO   Extra repo URL to clone alongside --repo (repeatable) —
                         cloned into the same workspace so the agent can read
                         across a repo group, e.g. a service split from a monorepo
  --dry-run             Analyze only — print summary without applying fixes
  --debug               Enable debug logging
  --model {{{cmds}}}
                        Claude model for all agents (default: sonnet)
"""


class CLIParseError(SystemExit):
    def __init__(self, message: str) -> None:
        print(f"jeanclode: error: {message}", file=sys.stderr)
        super().__init__(2)


def parse_args(argv: list[str] | None = None) -> CLIArgs:
    """Parse command-line arguments.

    First positional arg can be either a command name or a URL.
    If it matches a registered command, use it. Otherwise treat as URL.
    """
    from src.adaptors import list_commands
    from src.workflows import command_names

    args = argv if argv is not None else sys.argv[1:]
    known_commands = sorted(set(list_commands()) | command_names())

    if args and args[0] == "config":
        return CLIArgs(command="config")

    if not args or "-h" in args or "--help" in args:
        if not args:
            raise CLIParseError("expected a URL or command")
        print(_help_text(known_commands), end="")
        raise SystemExit(0)

    # Check if first arg is a known command
    adaptor_command: str | None = None
    if args[0] in known_commands:
        adaptor_command = args[0]
        args = args[1:]
        if not args:
            raise CLIParseError(f"'{adaptor_command}' requires at least one URL")

    dry_run = False
    debug = False
    model: str | None = None
    pending: list[str] = []
    current_repo: str | None = None
    current_related: list[str] = []
    finished: list[URLInput] = []

    def flush() -> None:
        nonlocal pending, current_repo, current_related
        for url in pending:
            finished.append(
                URLInput(url=url, repo=current_repo, related_repos=tuple(current_related))
            )
        pending = []
        current_repo = None
        current_related = []

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--dry-run":
            dry_run = True
        elif arg == "--debug":
            debug = True
        elif arg == "--model":
            i += 1
            if i >= len(args):
                raise CLIParseError("--model requires a value")
            model = args[i]
            if model not in _VALID_MODELS:
                raise CLIParseError(
                    f"argument --model: invalid choice: '{model}' "
                    f"(choose from {', '.join(sorted(_VALID_MODELS))})"
                )
        elif arg == "--repo":
            i += 1
            if i >= len(args):
                raise CLIParseError("--repo requires a value")
            if not pending:
                raise CLIParseError("--repo must follow at least one URL")
            current_repo = args[i]
        elif arg == "--related-repo":
            i += 1
            if i >= len(args):
                raise CLIParseError("--related-repo requires a value")
            if not pending:
                raise CLIParseError("--related-repo must follow at least one URL")
            current_related.append(args[i])
        elif arg.startswith("-"):
            raise CLIParseError(f"unrecognized argument: {arg}")
        else:
            # A bare URL starts a new batch — flush whatever repo/related-repo
            # flags applied to the previous one before starting the next.
            if pending and (current_repo is not None or current_related):
                flush()
            pending.append(arg)

        i += 1

    flush()

    if not finished:
        raise CLIParseError("expected at least one URL")

    return CLIArgs(
        command="run",
        adaptor_command=adaptor_command,
        urls=tuple(finished),
        dry_run=dry_run,
        debug=debug,
        model=model,
    )
