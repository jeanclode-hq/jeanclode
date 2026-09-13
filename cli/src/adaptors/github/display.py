"""GitHub display — maps code-review / pr-summary events to Rich UI."""

from __future__ import annotations

import json
import re
from typing import Any

from src.output import ProgressTracker


def _invokes_script(cmd: str, script_name: str) -> bool:
    """True iff the Bash command actually runs the plugin script via python.

    Subagents that read/grep the script source (e.g. when the PR being
    reviewed touches the skill itself) shouldn't trip step transitions —
    only the orchestrator's `python3 .../scripts/<name>.py` invocation does.
    """
    return f"scripts/{script_name}.py" in cmd and bool(re.search(r"\bpython\S*\s", cmd))


_AGENT_LABELS: dict[str, str] = {
    "issue-explorer": "Exploring linked issues...",
    "analyzer": "Running reviewers...",
    "synthesizer": "Synthesizing findings...",
    "deduplicator": "Deduplicating against discussions...",
    "fact-checker": "Fact-checking findings...",
    "styler": "Styling comments...",
    "summarizer": "Summarizing PR...",
    "summary-parser": "Refining summary...",
    "respond-agent": "Responding to mention...",
}

# Trail prefix for each subagent type (indexed when run in parallel —
# e.g. two ``review-analyzer`` launches become ``Reviewer[0]`` and
# ``Reviewer[1]`` so their interleaved tool activity is distinguishable).
_AGENT_PREFIX: dict[str, str] = {
    "issue-explorer": "Explorer",
    "analyzer": "Reviewer",
    "synthesizer": "Synth",
    "deduplicator": "Dedup",
    "fact-checker": "FactCheck",
    "styler": "Styler",
    "summarizer": "Summary",
    "summary-parser": "SummaryParser",
    "respond-agent": "Respond",
}

_SCRIPT_LABELS: dict[str, str] = {
    "fetch-pr-context": "Fetching PR context...",
    "filter-and-route": "Filtering...",
    "apply-guardrail": "Applying guardrail...",
    "style-comments": "Formatting comments...",
    "post-comments": "Posting comments...",
    "format-summary": "Formatting summary...",
    "post-summary": "Posting summary...",
    "post-reply": "Posting reply...",
    "resolve-thread": "Resolving thread...",
    "open-followup-issue": "Opening follow-up issue...",
    "relabel-pr": "Re-attaching label...",
    "format-result": "Formatting result...",
}


class GithubDisplay:
    def __init__(self) -> None:
        # Cache of styled comments (path/line/body) for the final review panel.
        self._styled_comments: list[dict[str, Any]] = []
        self._stopped_reason: str = ""
        # Subagent labeling: tool_use_id (the orchestrator's Agent block) →
        # trail prefix like ``Reviewer[0]``. ``_agent_counter`` indexes
        # parallel launches per prefix name. ``_last_agent_label`` deduplicates
        # ``on_agent_start`` so a single batch (e.g. two parallel reviewers)
        # produces one "Running reviewers..." step instead of two.
        self._subagent_prefix: dict[str, str] = {}
        self._agent_counter: dict[str, int] = {}
        self._last_agent_label: str = ""
        # Aggregated count for the current agent step (sums across parallel
        # subagents that share a label, e.g. two analyzers).
        self._step_finding_count: int = 0

    def on_agent_start(self, tracker: ProgressTracker, agent_info: str) -> None:
        info = agent_info.lower()
        label = ""
        for keyword, lbl in _AGENT_LABELS.items():
            if keyword in info:
                label = lbl
                break
        if not label:
            label = agent_info or "Subagent..."
        if label == self._last_agent_label:
            return  # parallel batch — same label already showing
        self._last_agent_label = label
        self._step_finding_count = 0
        tracker.step(label)

    def on_tool_call(
        self,
        tracker: ProgressTracker,
        tool_name: str,
        summary: str,
        input_dict: dict[str, Any],
        parent_tool_use_id: str | None,
        tool_use_id: str,
    ) -> None:
        # Subagent activity: prefix the trail entry with its label.
        prefix = self._subagent_prefix.get(parent_tool_use_id or "", "")
        tracker.on_tool_call(tool_name, summary, prefix=prefix)

        # Step transitions and prefix registration: orchestrator only.
        if parent_tool_use_id is not None:
            return

        if tool_name == "Agent":
            self._register_subagent(input_dict, tool_use_id)
            return

        if tool_name == "Bash":
            cmd = input_dict.get("command", "")
            for script_name, label in _SCRIPT_LABELS.items():
                if _invokes_script(cmd, script_name):
                    tracker.step(label)
                    self._last_agent_label = ""  # break agent dedup chain
                    break

    def _register_subagent(self, input_dict: dict[str, Any], tool_use_id: str) -> None:
        info = (input_dict.get("subagent_type") or input_dict.get("description") or "").lower()
        prefix_name = ""
        for keyword, name in _AGENT_PREFIX.items():
            if keyword in info:
                prefix_name = name
                break
        if not prefix_name:
            prefix_name = (info.split("-")[-1] or "Agent").capitalize()
        idx = self._agent_counter.get(prefix_name, 0)
        self._subagent_prefix[tool_use_id] = f"{prefix_name}[{idx}]"
        self._agent_counter[prefix_name] = idx + 1

    def on_tool_result(
        self,
        tracker: ProgressTracker,
        tool_name: str,
        output: str,
        input_dict: dict[str, Any],
        parent_tool_use_id: str | None,
    ) -> None:
        # Result handlers (counts, panels) are orchestrator-only.
        if parent_tool_use_id is not None:
            return

        if tool_name == "Agent":
            self._on_agent_result(tracker, output, input_dict)
            return

        if tool_name != "Bash":
            return

        cmd = input_dict.get("command", "")
        if _invokes_script(cmd, "filter-and-route"):
            self._on_filter_result(tracker, output)
        elif _invokes_script(cmd, "apply-guardrail"):
            self._on_guardrail_result(tracker, output)
        elif _invokes_script(cmd, "style-comments"):
            self._on_style_result(tracker, output)
        elif _invokes_script(cmd, "post-comments"):
            _show_post_panel(tracker, output)
            self._show_review_panel(tracker, output)
        elif _invokes_script(cmd, "post-summary"):
            _show_summary_panel(tracker, output)

    # -- agent result handlers --

    def _on_agent_result(
        self, tracker: ProgressTracker, output: str, input_dict: dict[str, Any]
    ) -> None:
        agent_type = (
            input_dict.get("subagent_type") or input_dict.get("description") or ""
        ).lower()
        data = _extract_json(output)
        if data is None:
            return

        if "analyzer" in agent_type or "synthesizer" in agent_type:
            comments = data.get("comments")
            if isinstance(comments, list):
                self._step_finding_count += len(comments)
                tracker.done(detail=_pluralize(self._step_finding_count, "finding"))
        elif "deduplicator" in agent_type or "fact-checker" in agent_type:
            keep = data.get("keep_indices")
            if isinstance(keep, list):
                tracker.done(detail=f"kept {len(keep)}")

    # -- bash result handlers --

    def _on_filter_result(self, tracker: ProgressTracker, output: str) -> None:
        data = _safe_json(output)
        if not isinstance(data, dict):
            return
        action = data.get("action", "")
        reason = data.get("reason", "")
        if action == "stop":
            tracker.done(detail=f"stopped: {reason}" if reason else "stopped")
            self._stopped_reason = reason or "filtered out"

    def _on_guardrail_result(self, tracker: ProgressTracker, output: str) -> None:
        data = _safe_json(output)
        if not isinstance(data, dict):
            return
        comments = data.get("comments")
        if isinstance(comments, list):
            tracker.done(detail=_pluralize(len(comments), "comment"))

    def _on_style_result(self, tracker: ProgressTracker, output: str) -> None:
        data = _safe_json(output)
        if not isinstance(data, dict):
            return
        comments = data.get("comments")
        if isinstance(comments, list):
            self._styled_comments = comments
            tracker.done(detail=_pluralize(len(comments), "comment"))

    # -- final review panel --

    def _show_review_panel(self, tracker: ProgressTracker, output: str) -> None:
        data = _safe_json(output)
        pr_url = data.get("pr_url", "") if isinstance(data, dict) else ""

        if not self._styled_comments:
            body = "[green]LGTM — no issues found[/]"
            if pr_url:
                body += f"\n[dim]{pr_url}[/]"
            tracker.panel(body, "Review", style="green")
            return

        # One panel per finding — the panel title is the file:line ref.
        for c in self._styled_comments:
            path = c.get("path") or c.get("new_path") or ""
            line = c.get("line") or c.get("new_line") or ""
            title = f"{path}:{line}" if path and line else (path or "comment")
            body = (c.get("body") or "").strip() or "[dim](empty)[/]"
            tracker.panel(body, title)
        if pr_url:
            tracker.panel(f"[bold]{pr_url}[/]", "PR", style="cyan")


# -- helpers --

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _try_direct(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _safe_json(text: str) -> Any:
    """Parse JSON from text. Tries direct parse, then a fenced block, then
    the first balanced ``{...}`` span. Useful when SDK / shells wrap stdout
    with extraneous text (timing notes, stderr lines, system reminders)."""
    if not text:
        return None
    direct = _try_direct(text)
    if direct is not None:
        return direct
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        parsed = _try_direct(fence.group(1))
        if parsed is not None:
            return parsed
    return _find_balanced_object(text)


def _find_balanced_object(text: str) -> Any:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    parsed = _try_direct(text[start : i + 1])
                    if parsed is not None:
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from agent output. Wrapper around ``_safe_json``
    that narrows to dict results."""
    parsed = _safe_json(text)
    return parsed if isinstance(parsed, dict) else None


def _pluralize(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _show_post_panel(tracker: ProgressTracker, output: str) -> None:
    data = _safe_json(output)
    if not isinstance(data, dict):
        return
    posted = data.get("posted", 0)
    failed = data.get("failed", 0)
    lgtm = data.get("lgtm", False)
    detail = "LGTM" if lgtm else _pluralize(posted, "posted")
    if failed:
        detail += f", {failed} failed"
    if data.get("loop_triggered"):
        detail += ", loop triggered"
    tracker.done(detail=detail)


def _show_summary_panel(tracker: ProgressTracker, output: str) -> None:
    data = _safe_json(output)
    if not isinstance(data, dict):
        return
    url = data.get("pr_url", "") or data.get("comment_url", "")
    if url:
        tracker.panel(f"[bold]{url}[/]", "Summary Posted", style="green")
