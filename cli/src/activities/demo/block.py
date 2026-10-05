"""The marked demo block in a PR/MR description.

It lives in the description, not a comment, so `pr_summary` has to lift it out
before the model rewrites the description and put it back after.
"""

from __future__ import annotations

import re

DEMO_START = "<!-- jeanclode:demo -->"
DEMO_END = "<!-- /jeanclode:demo -->"
_BLOCK_RE = re.compile(re.escape(DEMO_START) + r".*?" + re.escape(DEMO_END) + r"\n*", re.DOTALL)
# GitHub caps a description at 65536 characters; the recording matters more than its setup.
_MAX_DIFF_CHARS = 20_000


def split_demo_block(description: str) -> tuple[str, str]:
    """Return ``(block, description_without_it)``; ``block`` is "" when there is none."""
    match = _BLOCK_RE.search(description)
    if match is None:
        return "", description
    block = match.group(0).strip()
    rest = description[: match.start()] + description[match.end() :]
    return block, rest.strip("\n")


def with_demo_block(description: str, block: str) -> str:
    """Put ``block`` at the top of ``description``, replacing any earlier one."""
    _, rest = split_demo_block(description)
    if not block:
        return rest
    rest = rest.strip()
    return f"{block}\n\n{rest}\n" if rest else f"{block}\n"


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def render_demo_block(media_markdown: str, setup_diffs: dict[str, str]) -> str:
    parts = [DEMO_START, "#### Demo", "", media_markdown]
    diff = "\n".join(d.rstrip() for d in setup_diffs.values() if d.strip())
    if diff:
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS].rstrip() + "\n… (truncated)"
        fence = _fence(diff)
        parts += [
            "",
            "<details><summary>How this demo was set up</summary>",
            "",
            "Mocks, fixtures and config the demo used. None of it is part of this change.",
            "",
            f"{fence}diff\n{diff}\n{fence}",
            "",
            "</details>",
        ]
    parts.append(DEMO_END)
    return "\n".join(parts)


def render_demo_link(pr_url: str) -> str:
    """The block for a PR/MR that's part of the fix but not where the app runs."""
    return "\n".join(
        [
            DEMO_START,
            f"#### Demo\n\nThis change is part of a multi-repo fix: watch it working on {pr_url}.",
            DEMO_END,
        ]
    )
