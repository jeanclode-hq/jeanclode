"""Logging and console output — rich-based logging setup and output helpers."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

console = Console(stderr=True)


def setup_logging(*, debug: bool = False) -> None:
    """Configure logging with rich handler."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=debug)],
        force=True,
    )
    # Suppress noisy HTTP client debug logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_skip(reason: str) -> None:
    """Print a skip message."""
    console.print(f"[dim]Skipping:[/dim] {reason}")


def print_error(msg: str) -> None:
    """Print an error message."""
    console.print(f"[red]{msg}[/red]")


def print_success(msg: str) -> None:
    """Print a success message."""
    console.print(f"[green bold]{msg}[/green bold]")


def print_link(label: str, url: str) -> None:
    """Print a label with a clickable link."""
    console.print(f"[bold]{label}:[/bold] [link={url}]{url}[/link]")


def print_panel(content: str, *, title: str, border_style: str = "yellow") -> None:
    """Print content inside a bordered panel."""
    console.print(Panel(content, title=title, border_style=border_style))
