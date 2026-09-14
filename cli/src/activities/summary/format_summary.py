"""Render a refined summary into the final PR/MR comment body."""

from __future__ import annotations

import re

from src.activities.decorator import activity
from src.activities.summary.schemas import FileLine, ParsedSummary, SummaryPayload
from src.runtime.context import RunContext

# Marks the dropdown so a re-run can strip the previous one from the description it reads.
FILES_MARKER = "<!-- jeanclode:files -->"
_FILES_BLOCK_RE = re.compile(re.escape(FILES_MARKER) + r".*?</details>\s*", re.DOTALL)
_MAX_LINE_CHARS = 160


def strip_files_dropdown(description: str) -> str:
    return _FILES_BLOCK_RE.sub("", description)


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    if len(line) > _MAX_LINE_CHARS:
        line = line[: _MAX_LINE_CHARS - 1].rstrip() + "…"
    return line


def _cell(text: str) -> str:
    # Both platforms split table cells on "|", even inside code spans.
    return text.replace("|", "\\|")


def _render_file(file: FileLine) -> str:
    name = f"`{file.old_path}` → `{file.path}`" if file.old_path else f"`{file.path}`"
    return f"| {_cell(name)} | {_cell(_one_line(file.summary))} |"


def _render_files(files: list[FileLine]) -> str:
    rows = "\n".join(_render_file(f) for f in files)
    # GitHub and GitLab only render markdown inside <details> with blank lines around it.
    return (
        f"{FILES_MARKER}\n<details><summary>Changes per file ({len(files)})</summary>\n\n"
        f"| File | Content |\n|---|---|\n{rows}\n\n</details>\n"
    )


@activity(name="Formatting summary")
def format_summary(parsed: ParsedSummary, *, ctx: RunContext) -> SummaryPayload:  # noqa: ARG001
    body = "#### Description\n\n" + parsed.description.strip() + "\n"
    if parsed.files:
        body += "\n" + _render_files(parsed.files)
    return SummaryPayload(body=body)
