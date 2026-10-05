"""The marked demo block in a PR/MR description.

It lives in the description, not a comment, so `pr_summary` has to lift it out
before the model rewrites the description and put it back after.
"""

from __future__ import annotations

import re

DEMO_START = "<!-- jeanclode:demo -->"
DEMO_END = "<!-- /jeanclode:demo -->"
_BLOCK_RE = re.compile(re.escape(DEMO_START) + r".*?" + re.escape(DEMO_END) + r"\n*", re.DOTALL)
# GitHub caps a description at 65536 characters; the demo matters more than its setup.
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


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + "\n… (truncated)"


def render_demo_block(
    media_markdown: str,
    setup_diffs: dict[str, str],
    setup_files: dict[str, str] | None = None,
) -> str:
    parts = [DEMO_START, "#### Demo", "", media_markdown]
    setup: list[str] = []
    budget = _MAX_DIFF_CHARS
    diff = "\n".join(d.rstrip() for d in setup_diffs.values() if d.strip())
    if diff:
        diff = _truncate(diff, budget)
        budget -= len(diff)
        fence = _fence(diff)
        setup += ["", f"{fence}diff\n{diff}\n{fence}"]
    for name, content in (setup_files or {}).items():
        if budget <= 0:
            setup += ["", "… (more setup files truncated)"]
            break
        content = _truncate(content.rstrip(), budget)
        budget -= len(content)
        fence = _fence(content)
        lang = name.rsplit(".", 1)[-1] if "." in name else ""
        setup += ["", f"`{name}`", "", f"{fence}{lang}\n{content}\n{fence}"]
    if setup:
        parts += [
            "",
            "<details><summary>How this demo was set up</summary>",
            "",
            "Mocks, fixtures, config and the script the demo used. None of it is part of "
            "this change.",
            *setup,
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
