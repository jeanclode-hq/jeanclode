"""Parse GitLab MR URLs into (host, project_path, iid) triples.

Supports gitlab.com and self-hosted instances. Project path may include
nested groups (e.g. "group/subgroup/project").
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_MR_PATH_RE = re.compile(r"^/(?P<project>.+)/-/merge_requests/(?P<iid>\d+)/?$")
_ISSUE_PATH_RE = re.compile(r"^/(?P<project>.+)/-/(?:issues|work_items)/(?P<iid>\d+)/?$")
# GitLab note URL fragment — applies to both MR and issue note URLs.
_NOTE_FRAGMENT_RE = re.compile(r"#note_(?P<id>\d+)$")


def parse_mr_url(url: str) -> tuple[str, str, int] | None:
    """Parse a GitLab MR URL. Returns (host, project_path, iid) or None.

    Heuristic: host must contain "gitlab" (covers gitlab.com + self-hosted
    deployments named e.g. gitlab.example.com). For other hosts we'd need
    explicit configuration.
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        return None
    if "gitlab" not in parsed.netloc:
        return None
    match = _MR_PATH_RE.match(parsed.path)
    if not match:
        return None
    return parsed.netloc, match["project"], int(match["iid"])


def parse_issue_url(url: str) -> tuple[str, str, int] | None:
    """Parse a GitLab issue URL. Returns (host, project_path, iid) or None."""
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        return None
    if "gitlab" not in parsed.netloc:
        return None
    match = _ISSUE_PATH_RE.match(parsed.path)
    if not match:
        return None
    return parsed.netloc, match["project"], int(match["iid"])


def parse_note_url(url: str) -> tuple[str, str, int, str, int] | None:
    """Parse a GitLab MR/issue URL with a ``#note_N`` fragment.

    Returns ``(host, project_path, parent_iid, parent_kind, note_id)``
    where ``parent_kind`` is ``"merge_request"`` or ``"issue"``. Surface
    (top-level vs inline thread for MR notes) requires fetching the note
    from the API to inspect ``position``; that's the CLI fetcher's job.
    """
    fragment = _NOTE_FRAGMENT_RE.search(url)
    if not fragment:
        return None
    note_id = int(fragment["id"])

    mr = parse_mr_url(url)
    if mr is not None:
        host, project, iid = mr
        return host, project, iid, "merge_request", note_id

    issue = parse_issue_url(url)
    if issue is not None:
        host, project, iid = issue
        return host, project, iid, "issue", note_id

    return None


def has_note_fragment(url: str) -> bool:
    """True iff ``url`` ends in a GitLab ``#note_N`` fragment on an MR/issue."""
    return parse_note_url(url) is not None
