"""Fetch GitLab MR context (description, diff, discussions) into a workspace cache."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from src.adaptors.diffn import prepare_diff
from src.adaptors.discussions import (
    Comment,
    Discussions,
    ReviewSubmission,
    Thread,
    TopLevelComment,
    render_discussions,
)
from src.adaptors.gitlab.client import parse_mr_url, parse_note_url

logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _author_fields(author: dict | None) -> tuple[str, bool]:
    if not author:
        return "ghost", False
    username = author.get("username") or "unknown"
    is_bot = bool(author.get("bot"))
    return username, is_bot


def _parse_discussions(raw: list[dict], approvals: list[dict]) -> Discussions:
    threads: list[Thread] = []
    top_level: list[TopLevelComment] = []

    for d in raw:
        notes = [n for n in (d.get("notes") or []) if not n.get("system")]
        if not notes:
            continue

        # GitLab marks single comments with `individual_note: true`.
        if d.get("individual_note"):
            for n in notes:
                body = (n.get("body") or "").strip()
                if not body:
                    continue
                author, is_bot = _author_fields(n.get("author"))
                top_level.append(
                    TopLevelComment(
                        id=str(n.get("id") or ""),
                        author=author,
                        is_bot=is_bot,
                        created_at=n.get("created_at") or "",
                        body=body,
                    )
                )
            continue

        comments: list[Comment] = []
        path: str | None = None
        for n in notes:
            body = (n.get("body") or "").strip()
            if not body:
                continue
            position = n.get("position") or {}
            if position.get("new_path") and not path:
                path = position.get("new_path")
            author, is_bot = _author_fields(n.get("author"))
            comments.append(
                Comment(
                    id=str(n.get("id") or ""),
                    author=author,
                    is_bot=is_bot,
                    created_at=n.get("created_at") or "",
                    body=body,
                )
            )
        if not comments:
            continue

        # GitLab attaches `resolved` only to resolvable notes; non-resolvable
        # notes simply omit it, so a plain `any` is both correct and tolerant
        # of older API responses that don't expose the `resolvable` flag.
        is_resolved = any(n.get("resolved") for n in notes)
        threads.append(
            Thread(
                id=str(d.get("id") or ""),
                is_resolved=is_resolved,
                path=path,
                comments=comments,
            )
        )

    reviews: list[ReviewSubmission] = []
    for i, a in enumerate(approvals):
        user = a.get("user") or {}
        author, is_bot = _author_fields(user)
        uid = user.get("id")
        review_id = str(uid) if uid is not None else f"ghost-{i}"
        reviews.append(
            ReviewSubmission(
                id=review_id,
                author=author,
                is_bot=is_bot,
                created_at=a.get("created_at") or "",
                state="APPROVED",
                body="",
            )
        )

    return Discussions(threads=threads, reviews=reviews, top_level=top_level)


def _glab_paginate(endpoint: str, project: str, env: dict) -> list[dict]:
    """Walk a paginated `glab api` endpoint page-by-page.

    `glab api --paginate` concatenates one JSON array per page into stdout,
    which `json.loads` rejects on any response with more than one page. Driving
    pagination ourselves keeps each response a single JSON document.
    """
    nodes: list[dict] = []
    page = 1
    while True:
        proc = _run(
            [
                "glab",
                "api",
                "-R",
                project,
                f"{endpoint}?per_page=100&page={page}",
            ],
            env=env,
        )
        if proc.returncode != 0:
            logger.debug("glab api %s page %d failed: %s", endpoint, page, proc.stderr)
            return nodes
        try:
            chunk = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.debug("Failed to parse glab %s page %d", endpoint, page, exc_info=True)
            return nodes
        if not isinstance(chunk, list) or not chunk:
            return nodes
        nodes.extend(chunk)
        if len(chunk) < 100:
            return nodes
        page += 1


def _fetch_discussions(project: str, iid: str, env: dict) -> Discussions:
    slug = _glab_project_slug(project)
    raw = _glab_paginate(
        f"projects/{slug}/merge_requests/{iid}/discussions", project=project, env=env
    )

    approvals_proc = _run(
        [
            "glab",
            "api",
            "-R",
            project,
            f"projects/{slug}/merge_requests/{iid}/approvals",
        ],
        env=env,
    )
    approvals: list[dict] = []
    if approvals_proc.returncode == 0:
        try:
            data = json.loads(approvals_proc.stdout)
            if isinstance(data, dict):
                approvals = data.get("approved_by", []) or []
        except json.JSONDecodeError:
            logger.debug("Failed to parse glab approvals output", exc_info=True)

    return _parse_discussions(raw, approvals)


def fetch_mr_context(repo_dir: Path, url: str, token: str) -> None:
    """Populate <repo_dir>/.context/ with plain-text MR data files."""
    parsed = parse_mr_url(url)
    if not parsed:
        return
    host, project, iid = parsed
    iid_str = str(iid)
    env = {**os.environ, "GITLAB_TOKEN": token}
    if host != "gitlab.com":
        env["GITLAB_HOST"] = f"https://{host}"

    cache_dir = repo_dir / ".context"
    cache_dir.mkdir(parents=True, exist_ok=True)

    title = body = ""
    mr_author = ""
    view = _run(["glab", "mr", "view", iid_str, "-R", project, "-F", "json"], env=env)
    if view.returncode == 0:
        try:
            data = json.loads(view.stdout)
            title = data.get("title", "")
            body = data.get("description", "") or ""
            labels = data.get("labels", []) or []
            if labels:
                body = (body + f"\n\nLabels: {', '.join(labels)}").strip()
            mr_author = (data.get("author") or {}).get("username") or ""
        except json.JSONDecodeError:
            logger.debug("Failed to parse glab mr view output", exc_info=True)

    description = f"# {title}\n\n{body}".strip() if title else body

    diff_proc = _run(["glab", "mr", "diff", iid_str, "-R", project], env=env, timeout=120)
    diff = prepare_diff(diff_proc.stdout) if diff_proc.returncode == 0 else ""

    discussions = _fetch_discussions(project, iid_str, env)

    pr_url = f"https://{host}/{project}/-/merge_requests/{iid_str}"
    (cache_dir / "platform").write_text("gitlab\n")
    (cache_dir / "repo").write_text(f"{project}\n")
    (cache_dir / "pr").write_text(f"{iid_str}\n")
    (cache_dir / "pr_url").write_text(f"{pr_url}\n")
    (cache_dir / "pr_description").write_text(description)
    (cache_dir / "diff").write_text(diff)
    (cache_dir / "discussions").write_text(render_discussions(discussions))
    (cache_dir / "pr_author").write_text(f"{mr_author}\n")


# ---------------------------------------------------------------------------
# Respond context — derived entirely from a GitLab MR/issue URL with a
# ``#note_<id>`` fragment. The note ID is the only trigger identifier we
# need; the surface (top-level vs inline thread) comes from inspecting
# the note's ``position`` field returned by the API.
# ---------------------------------------------------------------------------


def _glab_api(path: str, env: dict) -> dict | None:
    proc = _run(["glab", "api", path], env=env, timeout=30)
    if proc.returncode != 0:
        logger.debug("glab api %s failed: %s", path, proc.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _glab_project_slug(project: str) -> str:
    """URL-encode a project path for the GitLab REST API."""
    from urllib.parse import quote

    return quote(project, safe="")


def _fetch_mr_author(project: str, iid: int, env: dict) -> str:
    proc = _run(["glab", "mr", "view", str(iid), "-R", project, "-F", "json"], env=env, timeout=30)
    if proc.returncode != 0:
        return "unknown"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "unknown"
    author = (data.get("author") or {}) if isinstance(data, dict) else {}
    return author.get("username") or "unknown"


def _write_text(path: Path, value: str) -> None:
    path.write_text(value if value.endswith("\n") else value + "\n")


def _find_note_via_discussions(
    project: str, parent_path: str, parent_iid: int, note_id: int, env: dict
) -> tuple[str, dict] | None:
    """Locate ``note_id`` among the parent's discussions.

    Returns ``(discussion_id, note)`` or ``None`` when the note can't be
    found. This is the *only* way to recover a note's thread: GitLab's
    flat single-note endpoints (``.../notes/:id``) do not expose
    ``discussion_id`` — the field exists solely on Discussions API
    objects — so reading it off a single-note response always yields
    an empty thread id.
    """
    slug = _glab_project_slug(project)
    discussions = _glab_paginate(
        f"projects/{slug}/{parent_path}/{parent_iid}/discussions", project=project, env=env
    )
    for discussion in discussions:
        for note in discussion.get("notes") or []:
            if str(note.get("id")) == str(note_id):
                return str(discussion.get("id") or ""), note
    return None


def _resolve_note(
    project: str, parent_path: str, parent_iid: int, note_id: int, env: dict
) -> tuple[str, dict | None]:
    """Return ``(thread_id, note)`` for the note that triggered this run.

    The Discussions API is the primary source — it yields the thread id
    and the note body in one pass. The flat single-note endpoint is only
    a fallback for when discussions can't be listed (permissions, API
    error): it still carries body/author/position, but never a thread
    id, so replies degrade to a flat top-level note.
    """
    found = _find_note_via_discussions(project, parent_path, parent_iid, note_id, env)
    if found is not None:
        return found

    logger.warning(
        "GitLab respond: note %s not found in %s/%s/%s discussions — "
        "falling back to the flat note endpoint (no thread id available)",
        note_id,
        project,
        parent_path,
        parent_iid,
    )
    slug = _glab_project_slug(project)
    note = _glab_api(f"projects/{slug}/{parent_path}/{parent_iid}/notes/{note_id}", env)
    return "", note


def fetch_respond_context(repo_dir: Path, url: str, token: str) -> None:
    """Populate ``<repo_dir>/.context/`` from a GitLab note URL."""
    parsed = parse_note_url(url)
    if not parsed:
        return
    host, project, parent_iid, parent_kind, note_id = parsed

    cache_dir = repo_dir / ".context"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GITLAB_TOKEN": token}
    if host != "gitlab.com":
        env["GITLAB_HOST"] = f"https://{host}"

    _write_text(cache_dir / "platform", "gitlab")
    _write_text(cache_dir / "repo", project)
    _write_text(cache_dir / "target_url", url)
    _write_text(cache_dir / "comment_id", str(note_id))

    is_mr = parent_kind == "merge_request"
    parent_path = "merge_requests" if is_mr else "issues"
    if is_mr:
        _write_text(cache_dir / "pr", str(parent_iid))
        _write_text(cache_dir / "pr_author", _fetch_mr_author(project, parent_iid, env))
    else:
        _write_text(cache_dir / "issue", str(parent_iid))
        _write_text(cache_dir / "surface", "issue")

    thread_id, note = _resolve_note(project, parent_path, parent_iid, note_id, env)
    if not note:
        logger.warning(
            "GitLab respond: could not fetch note %s on %s %s#%s — "
            ".context/ left without mention body, author or thread id",
            note_id,
            project,
            parent_path,
            parent_iid,
        )
        return

    if is_mr:
        # An inline diff note carries ``position``; a plain MR comment doesn't.
        surface = "pr_inline_thread" if note.get("position") else "pr_top_level"
        _write_text(cache_dir / "surface", surface)

    # GitLab uses ``discussion_id`` as the thread identifier across the API
    # (note creation, resolution). Empty means the reply can only be posted
    # as a flat, unthreaded note — log it, because that's a visible
    # degradation and the pod logs are the only place it shows up.
    _write_text(cache_dir / "thread_id", thread_id)
    if thread_id:
        logger.info("GitLab respond: note %s belongs to discussion %s", note_id, thread_id)
    else:
        logger.warning(
            "GitLab respond: no discussion id resolved for note %s on %s %s#%s — "
            "the reply will be posted as a flat top-level note",
            note_id,
            project,
            parent_path,
            parent_iid,
        )

    author_obj = note.get("author") or {}
    _write_text(cache_dir / "mention_author", author_obj.get("username") or "unknown")
    (cache_dir / "mention_body").write_text(note.get("body") or "")
