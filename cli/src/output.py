"""Progress tracking — rich spinner with tool trail and step timing.

Mirrors the predecessor project's TTY experience: a branded header panel, a live spinner
with the last 3 tool calls shown as an indented trail, and completed steps
printed with checkmarks, timing, and token usage.

In container mode (non-TTY), emits structured JSON log lines:
  [JEANCLODE:STEP]   — step lifecycle events (started, completed, failed)
  [JEANCLODE:RESULT] — final pipeline result
  [JEANCLODE:ERROR]  — fatal errors
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from src.agents.schemas import AgentUsage


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(tag: str, data: dict[str, Any], *, file: Any = None) -> None:
    """Write a structured JSON log line to stdout/stderr."""
    print(
        f"[{_timestamp()}] [JEANCLODE:{tag}] {json.dumps(data)}",
        file=file or sys.stdout,
        flush=True,
    )


def emit_step(
    step: str,
    status: str,
    *,
    detail: str = "",
    duration: float | None = None,
    issue_ids: list[str] | None = None,
    usage: AgentUsage | None = None,
    error: str = "",
) -> None:
    """Emit a structured step event for the container watcher.

    Args:
        step: Step name (e.g. "auth", "clone", "triage", "plan", "fix").
        status: One of "started", "completed", "failed", "skipped".
        detail: Optional human-readable detail.
        duration: Step duration in seconds (set on completed/failed).
        issue_ids: Issue IDs relevant to this step.
        usage: Token usage for this step.
        error: Error message (set on failed).
    """
    data: dict[str, Any] = {"step": step, "status": status}
    if detail:
        data["detail"] = detail
    if duration is not None:
        data["duration_s"] = round(duration, 1)
    if issue_ids:
        data["issue_ids"] = issue_ids
    if usage and (usage.input_tokens or usage.output_tokens):
        data["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
        }
    if error:
        data["error"] = error
    _emit("STEP", data)


def emit_triage(issue_id: str, record: dict[str, Any]) -> None:
    """Emit a full, untruncated triage record for the audit log.

    Captures the structured triage decision per issue (including skill
    attribution in the ``reason`` field) at the moment filter-and-route
    consumes it. Downstream watchers can ingest this for governance —
    e.g. detecting when a skill flipped ``actionable=false``.
    """
    _emit("TRIAGE", {"issue_id": issue_id, "record": record})


def emit_tool(tool: str, summary: str, *, result: str = "") -> None:
    """Emit a structured per-tool-call event for the container watcher.

    Logged on every tool invocation by a subagent or orchestrator, so debug
    transcripts can show exactly what the model did. ``summary`` is the
    short input description; ``result`` is the truncated tool output.
    """
    data: dict[str, str] = {"tool": tool}
    if summary:
        data["summary"] = summary[:300]
    if result:
        data["result"] = result[:300]
    _emit("TOOL", data)


def emit_result(data: dict[str, Any], *, is_tty: bool = False) -> None:
    """Emit structured result for container watcher.

    Suppressed in TTY mode since humans see the ProgressTracker output instead.
    """
    if is_tty:
        return
    _emit("RESULT", data)


def emit_error(
    error_type: str,
    message: str,
    *,
    is_tty: bool = False,
    usage: AgentUsage | None = None,
) -> None:
    """Emit structured error for container watcher.

    Suppressed in TTY mode — callers use ProgressTracker.fail() instead.
    """
    if is_tty:
        return
    data: dict[str, Any] = {"type": error_type, "message": message}
    if usage and (usage.input_tokens or usage.output_tokens):
        data["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
        }
    _emit("ERROR", data, file=sys.stderr)


_RETRY_AFTER_PATTERN = re.compile(r"retry.after[^0-9]{0,10}(\d+(?:\.\d+)?)", re.IGNORECASE)


def extract_retry_after(exc: BaseException) -> float | None:
    """Best-effort ``retry-after`` (seconds) from a rate-limit exception.

    Tries a ``retry_after`` attribute first (in case the SDK ever surfaces
    one directly), then falls back to scanning the exception's message —
    the API error body is folded into ``str(exc)`` upstream. Returns
    ``None`` when nothing is found; the backend watcher then falls back to
    its own 5-hour default (ADR-010) rather than trusting a guess here.
    """
    value = getattr(exc, "retry_after", None)
    if isinstance(value, int | float):
        return float(value)
    match = _RETRY_AFTER_PATTERN.search(str(exc))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def emit_rate_limit_error(
    message: str,
    *,
    retry_after: float | None = None,
    branch: str = "",
    pr_url: str = "",
) -> None:
    """Emit a distinct, identifiable rate-limit event for the backend watcher.

    ADR-010 treats the real, documented ``429 rate_limit_error`` as the only
    authoritative exhaustion signal — folding it into a generic error log
    line would leave the watcher with no way to tell "temporarily out of
    capacity" from "the run genuinely failed". Carries whichever LLM
    credential was in use (``JEANCLODE_LLM_CREDENTIAL_ID``, set by the
    backend at dispatch time — absent when running against an env-var
    credential outside the pool) and, for ``sentry_fix`` specifically, the
    branch/PR URL in scope for the group that failed — only known here, at
    the moment of failure, not reconstructible by the watcher afterwards.

    Always emitted regardless of TTY: unlike ``emit_error``, there is no
    human-facing equivalent for this signal, and container mode (the only
    place the watcher parses it) is never a TTY anyway.
    """
    data: dict[str, Any] = {"type": "rate_limit_error", "message": message}
    credential_id = os.environ.get("JEANCLODE_LLM_CREDENTIAL_ID")
    if credential_id:
        data["credential_id"] = credential_id
    if retry_after is not None:
        data["retry_after"] = retry_after
    if branch:
        data["branch"] = branch
    if pr_url:
        data["pr_url"] = pr_url
    _emit("ERROR", data, file=sys.stderr)


def format_usage(usage: AgentUsage) -> str:
    """Format agent usage as a compact string for step display."""
    parts = [
        f"{usage.input_tokens:,} in",
        f"{usage.output_tokens:,} out",
    ]
    if usage.cache_read_tokens:
        parts.append(f"{usage.cache_read_tokens:,} cache read")
    if usage.cache_write_tokens:
        parts.append(f"{usage.cache_write_tokens:,} cache write")
    return " \u00b7 ".join(parts)


# Only show these tool names in the trail
_DISPLAY_TOOLS = {"Read", "Grep", "Glob", "Bash"}


class _SpinnerWithTrail:
    """Renderable: spinner line + indented tool trail lines below."""

    def __init__(self) -> None:
        self.spinner = Spinner("dots", text="")
        self.trail: list[str] = []

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        frame = self.spinner.render(console.get_time())
        line = Text("  ")
        line.append_text(frame)
        yield line
        for trail_line in self.trail:
            yield Text(f"      {trail_line}", style="dim")


class ProgressTracker:
    """Rich-based CLI progress tracker.

    Prints a branded header, then shows each pipeline step with a spinner
    while active and a checkmark (with detail + duration) once completed.
    Tool calls appear as indented lines below the spinner during execution.
    """

    def __init__(
        self,
        issue_url: str,
        version: str,
        *,
        model: str = "",
        console: Console | None = None,
    ) -> None:
        self._issue_url = issue_url
        self._version = version
        self._model = model
        self._skill: str = ""
        self._console = console or Console(stderr=True, highlight=False)

        self._live: Live | None = None
        self._display: _SpinnerWithTrail | None = None
        self._tool_trail: deque[str] = deque(maxlen=3)
        self._current_step: str | None = None
        self._step_detail: str = ""
        self._step_usage: str = ""
        self._step_start: float = 0.0
        self._total_start: float = 0.0

    def set_skill(self, skill: str) -> None:
        """Set the skill name (shown in the header panel)."""
        self._skill = skill

    def start(self) -> None:
        """Print the header panel and start the live display."""
        self._total_start = time.time()
        lines = [self._issue_url]
        if self._skill:
            lines.append(f"[dim]skill:[/] {self._skill}")
        if self._model:
            lines.append(f"[dim]model:[/] {self._model}")
        body = Text.from_markup("\n".join(lines))
        self._console.print()
        self._console.print(
            Panel(
                body,
                title=f"[bold]JeanClode[/] [dim]v{self._version}[/]",
                title_align="left",
                border_style="cyan",
                expand=False,
                padding=(0, 1),
            )
        )
        self._display = _SpinnerWithTrail()
        self._display.spinner.update(text="[bold cyan]Initializing...[/]")
        self._live = Live(
            self._display, console=self._console, refresh_per_second=12, transient=True
        )
        self._live.start()

    def step(self, msg: str) -> None:
        """Start a new pipeline step."""
        if self._live is None:
            return
        if self._current_step is not None:
            self._print_completed()
        self._current_step = msg
        self._step_detail = ""
        self._step_usage = ""
        self._step_start = time.time()
        self._tool_trail.clear()
        if self._display is not None:
            self._display.spinner.update(text=f"[bold cyan]{msg}[/]")
            self._display.trail = []
        # Restart live display if it was stopped (e.g. after panel())
        if self._live is not None and not self._live.is_started:
            self._live.start()

    def set_step_label(self, msg: str) -> None:
        """Update the current step's spinner label without finalizing it.

        Used when a step's *contents* change but the step itself hasn't
        completed — e.g. when a second parallel agent joins a running
        step, the label widens from ``Running A...`` to ``Running A +
        B...`` without resetting the timer or wiping the tool trail.
        """
        if self._live is None or self._current_step is None:
            return
        self._current_step = msg
        if self._display is not None:
            self._display.spinner.update(text=f"[bold cyan]{msg}[/]")

    def done(self, detail: str = "", usage: str = "") -> None:
        """Attach a result detail and usage to the current step."""
        self._step_detail = detail
        if usage:
            self._step_usage = usage

    def on_tool_call(self, tool_name: str, summary: str = "", prefix: str = "") -> None:
        """Record a tool call in the trail (shown below the spinner)."""
        if tool_name not in _DISPLAY_TOOLS:
            return
        parts = [p for p in (prefix, tool_name, summary) if p]
        label = " ".join(parts)
        self._tool_trail.append(label)
        if self._display is not None:
            self._display.trail = list(self._tool_trail)

    def make_tool_callback(self, prefix: str = "") -> Callable[[str, str, dict], None]:
        """Return a closure suitable for BaseAgent on_tool_call.

        Args:
            prefix: Optional label prepended to each trail line (e.g. "Triage[0]").
        """

        def _callback(tool_name: str, summary: str, _input_dict: dict) -> None:
            self.on_tool_call(tool_name, summary, prefix=prefix)

        return _callback

    def finish(self) -> None:
        """Stop live display, print last completed step, and show total duration."""
        if self._live is None:
            return
        self._live.stop()
        self._print_completed()
        self._console.print()
        self._live = None
        self._display = None

    def fail(self, error: str, usage: AgentUsage | None = None) -> None:
        """Stop live display and show current step as failed."""
        if self._live is None:
            return
        self._live.stop()
        self._console.print(f"  [red]\u2717[/] {self._current_step}")
        self._console.print(f"\n  [bold red]Error: {error}[/]")
        if usage and (usage.input_tokens or usage.output_tokens):
            self._console.print(f"  [dim]{format_usage(usage)}[/]")
        self._console.print()
        self._live = None
        self._display = None

    def panel(self, content: str, title: str, style: str = "cyan") -> None:
        """Print a Rich panel below the progress output.

        Flushes the pending completed step first, and pauses/resumes the live
        display so the panel renders cleanly.
        """
        was_live = self._live is not None
        if self._live is not None and was_live:
            self._live.stop()
        self._print_completed()
        self._console.print(
            Panel(
                Text.from_markup(content),
                title=f"[bold]{title}[/]",
                title_align="left",
                border_style=style,
                padding=(0, 1),
            )
        )
        # Don't restart live here — the transient display would overwrite
        # the panel's bottom border. Next step() call will restart it.

    def summary(self, usage: AgentUsage) -> None:
        """Print total usage summary. Flushes pending step and stops the live display."""
        if self._live is not None:
            self._live.stop()
            self._print_completed()
            self._live = None
            self._display = None
        elapsed = time.time() - self._total_start
        self._console.print(f"\n  [bold green]\u2713 Done in {elapsed:.1f}s[/]")
        self._console.print(f"  [dim]{format_usage(usage)}[/]")
        self._console.print()

    def _print_completed(self) -> None:
        if self._current_step is not None:
            elapsed = time.time() - self._step_start
            detail = f" [dim]{self._step_detail}[/]" if self._step_detail else ""
            usage_str = f" [dim]{self._step_usage}[/]" if self._step_usage else ""
            self._console.print(
                f"  [green]\u2713[/] {self._current_step}{detail}"
                f" [dim]({elapsed:.1f}s)[/]{usage_str}"
            )
            self._current_step = None
