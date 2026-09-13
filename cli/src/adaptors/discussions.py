"""Shared discussion model and renderer for PR/MR caches.

The shape is symmetric across GitHub and GitLab — same rendered markdown
sections so the deduplicator agent reads one format. Platform-specific IDs
are preserved verbatim so they're directly usable in follow-up CLI calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Comment(BaseModel):
    id: str
    author: str
    is_bot: bool
    created_at: str
    body: str


class Thread(BaseModel):
    id: str
    is_resolved: bool
    path: str | None = None
    comments: list[Comment] = Field(default_factory=list)


class ReviewSubmission(BaseModel):
    id: str
    author: str
    is_bot: bool
    created_at: str
    state: str
    body: str


class TopLevelComment(BaseModel):
    id: str
    author: str
    is_bot: bool
    created_at: str
    body: str


class Discussions(BaseModel):
    threads: list[Thread] = Field(default_factory=list)
    reviews: list[ReviewSubmission] = Field(default_factory=list)
    top_level: list[TopLevelComment] = Field(default_factory=list)


def _role(is_bot: bool) -> str:
    return "bot" if is_bot else "human"


def _format_thread(thread: Thread) -> str:
    state = "RESOLVED" if thread.is_resolved else "OPEN"
    header = f"### Thread {thread.id} [{state}]"
    if thread.path:
        header += f" on `{thread.path}`"
    lines = [header]
    for c in thread.comments:
        body = c.body.strip().replace("\n", " ")
        lines.append(f"- **{c.author}** ({_role(c.is_bot)}, {c.created_at}) [{c.id}]: {body}")
    return "\n".join(lines)


def _format_review(review: ReviewSubmission) -> str:
    header = (
        f"### Review {review.id} by **{review.author}** "
        f"({_role(review.is_bot)}, {review.created_at}) [{review.state}]"
    )
    body = review.body.strip()
    return f"{header}\n\n{body}" if body else header


def _format_top_level(c: TopLevelComment) -> str:
    body = c.body.strip().replace("\n", " ")
    return f"- **{c.author}** ({_role(c.is_bot)}, {c.created_at}) [{c.id}]: {body}"


def render_discussions(d: Discussions) -> str:
    """Render a Discussions model as plain markdown for inlining via `cat`."""
    parts: list[str] = []

    parts.append("## Review threads\n")
    if d.threads:
        parts.append("\n\n".join(_format_thread(t) for t in d.threads))
    else:
        parts.append("_None._")

    parts.append("\n\n## Review submissions\n")
    if d.reviews:
        parts.append("\n\n".join(_format_review(r) for r in d.reviews))
    else:
        parts.append("_None._")

    parts.append("\n\n## Top-level comments\n")
    if d.top_level:
        parts.append("\n".join(_format_top_level(c) for c in d.top_level))
    else:
        parts.append("_None._")

    return "".join(parts) + "\n"
