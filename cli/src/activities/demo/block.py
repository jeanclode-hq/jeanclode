"""The marked demo block in a PR/MR description.

It lives in the description, not a comment, so `pr_summary` has to lift it out
before the model rewrites the description and put it back after.
"""

from __future__ import annotations

import re

DEMO_START = "<!-- jeanclode:demo -->"
DEMO_END = "<!-- /jeanclode:demo -->"
_BLOCK_RE = re.compile(re.escape(DEMO_START) + r".*?" + re.escape(DEMO_END) + r"\n*", re.DOTALL)


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


def render_demo_block(media_markdown: str) -> str:
    return "\n".join([DEMO_START, "#### Demo", "", media_markdown, DEMO_END])


def render_demo_link(pr_url: str) -> str:
    """The block for a PR/MR that's part of the fix but not where the app runs."""
    return "\n".join(
        [
            DEMO_START,
            f"#### Demo\n\nThis change is part of a multi-repo fix: watch it working on {pr_url}.",
            DEMO_END,
        ]
    )
