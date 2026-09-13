"""Parse GitHub PR / issue / comment URLs into structured tuples."""

from __future__ import annotations

import re
from enum import StrEnum

_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)

_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)


# Comment URL fragments — GitHub embeds the surface and the comment's
# numeric DB ID directly in the fragment. We use that as the sole
# trigger identifier for the respond workflow (no env vars needed).
_COMMENT_FRAGMENT_RE = re.compile(
    r"#(?P<kind>issuecomment|discussion_r|pullrequestreview)-?(?P<id>\d+)$"
)


class CommentSurface(StrEnum):
    """Which respond surface a GitHub comment URL points at."""

    PR_TOP_LEVEL = "pr_top_level"  # /pull/N#issuecomment-X
    PR_INLINE_THREAD = "pr_inline_thread"  # /pull/N#discussion_r-X
    PR_REVIEW_SUBMISSION = "pr_review_submission"  # /pull/N#pullrequestreview-X
    ISSUE = "issue"  # /issues/N#issuecomment-X


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """Parse a GitHub PR URL. Returns (owner, repo, number) or None."""
    match = _PR_URL_RE.match(url)
    if not match:
        return None
    return match["owner"], match["repo"], int(match["number"])


def parse_issue_url(url: str) -> tuple[str, str, int] | None:
    """Parse a GitHub issue URL. Returns (owner, repo, number) or None."""
    match = _ISSUE_URL_RE.match(url)
    if not match:
        return None
    return match["owner"], match["repo"], int(match["number"])


def parse_comment_url(
    url: str,
) -> tuple[str, str, int, CommentSurface, int] | None:
    """Parse a GitHub PR/issue URL with a comment fragment.

    Returns ``(owner, repo, parent_number, surface, comment_id)`` or
    ``None`` if the URL has no recognized comment fragment.

    The comment ID is the numeric DB ID encoded in the fragment — this is
    the only identifier we need to fetch the comment back from the API.
    The thread node ID (for inline-thread resolution) is looked up later
    by walking the PR's review-thread cache.
    """
    fragment_match = _COMMENT_FRAGMENT_RE.search(url)
    if not fragment_match:
        return None
    kind = fragment_match["kind"]
    comment_id = int(fragment_match["id"])

    pr = parse_pr_url(url)
    if pr is not None:
        owner, repo, number = pr
        if kind == "discussion_r":
            surface = CommentSurface.PR_INLINE_THREAD
        elif kind == "pullrequestreview":
            surface = CommentSurface.PR_REVIEW_SUBMISSION
        elif kind == "issuecomment":
            surface = CommentSurface.PR_TOP_LEVEL
        else:
            return None
        return owner, repo, number, surface, comment_id

    issue = parse_issue_url(url)
    if issue is not None and kind == "issuecomment":
        owner, repo, number = issue
        return owner, repo, number, CommentSurface.ISSUE, comment_id

    return None


def has_comment_fragment(url: str) -> bool:
    """True iff ``url`` ends in a recognized GitHub comment fragment."""
    return parse_comment_url(url) is not None
