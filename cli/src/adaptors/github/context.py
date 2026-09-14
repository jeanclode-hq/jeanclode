"""Fetch GitHub PR context (description, diff, discussions) into a workspace cache."""

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
from src.adaptors.github.client import (
    CommentSurface,
    parse_comment_url,
    parse_pr_url,
)

logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          path
          comments(first: 100) {
            nodes {
              id
              databaseId
              createdAt
              bodyText
              author { __typename login }
            }
          }
        }
      }
    }
  }
}
"""

_REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviews(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          state
          bodyText
          submittedAt
          author { __typename login }
        }
      }
    }
  }
}
"""

_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      comments(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          createdAt
          bodyText
          author { __typename login }
        }
      }
    }
  }
}
"""


def _author_fields(author: dict | None) -> tuple[str, bool]:
    if not author:
        return "ghost", False
    login = author.get("login") or "unknown"
    typename = author.get("__typename") or ""
    is_bot = typename == "Bot" or login.endswith("[bot]")
    return login, is_bot


def _parse_threads(thread_nodes: list[dict]) -> list[Thread]:
    threads: list[Thread] = []
    for node in thread_nodes:
        comments_raw = (node.get("comments") or {}).get("nodes", []) or []
        comments: list[Comment] = []
        for c in comments_raw:
            body = (c.get("bodyText") or "").strip()
            if not body:
                continue
            author, is_bot = _author_fields(c.get("author"))
            comments.append(
                Comment(
                    id=c.get("id") or "",
                    author=author,
                    is_bot=is_bot,
                    created_at=c.get("createdAt") or "",
                    body=body,
                )
            )
        if not comments:
            continue
        threads.append(
            Thread(
                id=node.get("id") or "",
                is_resolved=bool(node.get("isResolved")),
                path=node.get("path"),
                comments=comments,
            )
        )

    return threads


def _parse_reviews(review_nodes: list[dict]) -> list[ReviewSubmission]:
    reviews: list[ReviewSubmission] = []
    for r in review_nodes:
        body = (r.get("bodyText") or "").strip()
        state = r.get("state") or "COMMENTED"
        if not body and state == "COMMENTED":
            continue
        author, is_bot = _author_fields(r.get("author"))
        reviews.append(
            ReviewSubmission(
                id=r.get("id") or "",
                author=author,
                is_bot=is_bot,
                created_at=r.get("submittedAt") or "",
                state=state,
                body=body,
            )
        )
    return reviews


def _parse_top_level(comment_nodes: list[dict]) -> list[TopLevelComment]:
    top_level: list[TopLevelComment] = []
    for c in comment_nodes:
        body = (c.get("bodyText") or "").strip()
        if not body:
            continue
        author, is_bot = _author_fields(c.get("author"))
        top_level.append(
            TopLevelComment(
                id=c.get("id") or "",
                author=author,
                is_bot=is_bot,
                created_at=c.get("createdAt") or "",
                body=body,
            )
        )
    return top_level


def _paginate_connection(
    query: str, key: str, *, owner: str, repo: str, pr: int, env: dict
) -> list[dict]:
    """Walk a paginated `pullRequest.<key>` GraphQL connection until exhausted."""
    nodes: list[dict] = []
    cursor: str | None = None
    while True:
        # `-f` for String! params (owner, repo, after); `-F` for Int! (pr).
        # `-F` auto-coerces numeric-looking strings to ints, which would break
        # GraphQL type-checking on `String!` variables.
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"repo={repo}",
            "-F",
            f"pr={pr}",
        ]
        if cursor:
            cmd += ["-f", f"after={cursor}"]
        proc = _run(cmd, env=env, timeout=60)
        if proc.returncode != 0:
            logger.debug("gh api graphql %s failed: %s", key, proc.stderr)
            return nodes
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.debug("Failed to parse gh api graphql output for %s", key, exc_info=True)
            return nodes
        conn = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
            key
        ) or {}
        nodes.extend(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return nodes
        cursor = page_info.get("endCursor")
        if not cursor:
            return nodes


def _fetch_discussions(owner: str, repo: str, pr: int, env: dict) -> Discussions:
    thread_nodes = _paginate_connection(
        _THREADS_QUERY, "reviewThreads", owner=owner, repo=repo, pr=pr, env=env
    )
    review_nodes = _paginate_connection(
        _REVIEWS_QUERY, "reviews", owner=owner, repo=repo, pr=pr, env=env
    )
    comment_nodes = _paginate_connection(
        _COMMENTS_QUERY, "comments", owner=owner, repo=repo, pr=pr, env=env
    )
    return Discussions(
        threads=_parse_threads(thread_nodes),
        reviews=_parse_reviews(review_nodes),
        top_level=_parse_top_level(comment_nodes),
    )


def fetch_pr_context(repo_dir: Path, url: str, token: str) -> None:
    """Populate <repo_dir>/.context/ with plain-text PR data files.

    Cache lives inside the cloned repo so agents can use the simple relative
    path `.context/<file>` (no `..` traversal). Files are plain text — no
    JSON parsing needed in the prompts.
    """
    parsed = parse_pr_url(url)
    if not parsed:
        return
    owner, repo, pr = parsed
    repo_full = f"{owner}/{repo}"
    pr_str = str(pr)
    env = {**os.environ, "GH_TOKEN": token}

    cache_dir = repo_dir / ".context"
    cache_dir.mkdir(parents=True, exist_ok=True)

    title = body = ""
    view = _run(
        ["gh", "pr", "view", pr_str, "-R", repo_full, "--json", "title,body,labels"], env=env
    )
    if view.returncode == 0:
        try:
            data = json.loads(view.stdout)
            title = data.get("title", "")
            body = data.get("body", "") or ""
            labels = [lb.get("name", "") for lb in data.get("labels", []) if lb.get("name")]
            if labels:
                body = (body + f"\n\nLabels: {', '.join(labels)}").strip()
        except json.JSONDecodeError:
            logger.debug("Failed to parse gh pr view output", exc_info=True)

    description = f"# {title}\n\n{body}".strip() if title else body

    diff_proc = _run(["gh", "pr", "diff", pr_str, "-R", repo_full], env=env, timeout=120)
    diff = prepare_diff(diff_proc.stdout) if diff_proc.returncode == 0 else ""

    discussions = _fetch_discussions(owner, repo, pr, env)
    pr_author = _fetch_pr_author(repo_full, pr, env)

    pr_url = f"https://github.com/{repo_full}/pull/{pr_str}"
    (cache_dir / "platform").write_text("github\n")
    (cache_dir / "repo").write_text(f"{repo_full}\n")
    (cache_dir / "pr").write_text(f"{pr_str}\n")
    (cache_dir / "pr_url").write_text(f"{pr_url}\n")
    (cache_dir / "pr_description").write_text(description)
    (cache_dir / "diff").write_text(diff)
    (cache_dir / "discussions").write_text(render_discussions(discussions))
    (cache_dir / "pr_author").write_text(f"{pr_author}\n")


# ---------------------------------------------------------------------------
# Respond context — derived entirely from the comment URL.
#
# The CLI's argument is a URL like:
#   https://github.com/o/r/pull/123#discussion_r4567
#   https://github.com/o/r/pull/123#issuecomment-4567
#   https://github.com/o/r/pull/123#pullrequestreview-4567
#   https://github.com/o/r/issues/12#issuecomment-4567
#
# The fragment encodes the surface and the comment's numeric ID; the rest
# (body, author, thread node ID for inline-thread resolution) we fetch
# from GitHub here. No env vars cross the backend → container boundary —
# the backend just hands us the URL.
# ---------------------------------------------------------------------------


def _gh_api(path: str, env: dict) -> dict | None:
    proc = _run(["gh", "api", path], env=env, timeout=30)
    if proc.returncode != 0:
        logger.debug("gh api %s failed: %s", path, proc.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _fetch_pr_author(repo_full: str, pr_number: int, env: dict) -> str:
    proc = _run(
        ["gh", "pr", "view", str(pr_number), "-R", repo_full, "--json", "author"],
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        return "unknown"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "unknown"
    author = (data.get("author") or {}) if isinstance(data, dict) else {}
    return author.get("login") or "unknown"


def _resolve_inline_thread(
    *, owner: str, repo: str, pr: int, comment_id: int, env: dict
) -> dict | None:
    """Walk reviewThreads to find the thread containing ``comment_id``.

    Returns the matching ``(thread, comment)`` raw GraphQL nodes or None.
    Comments here carry both ``id`` (GraphQL node ID) and ``databaseId``
    (numeric ID encoded in the URL fragment).
    """
    threads = _paginate_connection(
        _THREADS_QUERY, "reviewThreads", owner=owner, repo=repo, pr=pr, env=env
    )
    for thread in threads:
        for c in (thread.get("comments") or {}).get("nodes") or []:
            if c.get("databaseId") == comment_id:
                return {"thread": thread, "comment": c}
    return None


def _write_text(path: Path, value: str) -> None:
    """Write a single field. Trailing newline so ``cat`` is friendly."""
    path.write_text(value if value.endswith("\n") else value + "\n")


def _populate_inline_thread(
    cache_dir: Path,
    *,
    owner: str,
    repo: str,
    pr: int,
    comment_id: int,
    env: dict,
) -> None:
    found = _resolve_inline_thread(owner=owner, repo=repo, pr=pr, comment_id=comment_id, env=env)
    _write_text(cache_dir / "comment_id", str(comment_id))
    if not found:
        return
    thread = found["thread"]
    comment = found["comment"]
    author, _ = _author_fields(comment.get("author"))
    _write_text(cache_dir / "thread_id", thread.get("id") or "")
    _write_text(cache_dir / "thread_resolved", "true" if thread.get("isResolved") else "false")
    _write_text(cache_dir / "thread_path", thread.get("path") or "")
    (cache_dir / "mention_body").write_text(comment.get("bodyText") or "")
    _write_text(cache_dir / "mention_author", author)


def _populate_review_submission(
    cache_dir: Path,
    *,
    owner: str,
    repo: str,
    pr: int,
    comment_id: int,
    env: dict,
) -> None:
    data = _gh_api(f"/repos/{owner}/{repo}/pulls/{pr}/reviews/{comment_id}", env)
    _write_text(cache_dir / "comment_id", str(comment_id))
    if not data:
        return
    user = data.get("user") or {}
    login = user.get("login") or "unknown"
    (cache_dir / "mention_body").write_text(data.get("body") or "")
    _write_text(cache_dir / "mention_author", login)


def _populate_issue_or_top_level_comment(
    cache_dir: Path,
    *,
    repo_full: str,
    comment_id: int,
    env: dict,
) -> None:
    """REST issue-comments endpoint covers both PR top-level and issue comments."""
    data = _gh_api(f"/repos/{repo_full}/issues/comments/{comment_id}", env)
    _write_text(cache_dir / "comment_id", str(comment_id))
    if not data:
        return
    user = data.get("user") or {}
    login = user.get("login") or "unknown"
    (cache_dir / "mention_body").write_text(data.get("body") or "")
    _write_text(cache_dir / "mention_author", login)


def fetch_respond_context(repo_dir: Path, url: str, token: str) -> None:
    """Populate ``<repo_dir>/.context/`` from a GitHub comment URL.

    URL fragment determines the surface and comment ID; the rest we fetch
    from GitHub. No-ops cleanly for non-comment URLs (local
    ``jeanclode review …`` runs etc.).
    """
    parsed = parse_comment_url(url)
    if not parsed:
        return
    owner, repo, parent_number, surface, comment_id = parsed

    cache_dir = repo_dir / ".context"
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_full = f"{owner}/{repo}"
    env = {**os.environ, "GH_TOKEN": token}

    _write_text(cache_dir / "platform", "github")
    _write_text(cache_dir / "repo", repo_full)
    _write_text(cache_dir / "surface", surface.value)
    _write_text(cache_dir / "target_url", url)

    if surface == CommentSurface.ISSUE:
        _write_text(cache_dir / "issue", str(parent_number))
        _populate_issue_or_top_level_comment(
            cache_dir, repo_full=repo_full, comment_id=comment_id, env=env
        )
        return

    _write_text(cache_dir / "pr", str(parent_number))
    _write_text(cache_dir / "pr_author", _fetch_pr_author(repo_full, parent_number, env))

    if surface == CommentSurface.PR_INLINE_THREAD:
        _populate_inline_thread(
            cache_dir,
            owner=owner,
            repo=repo,
            pr=parent_number,
            comment_id=comment_id,
            env=env,
        )
    elif surface == CommentSurface.PR_REVIEW_SUBMISSION:
        _populate_review_submission(
            cache_dir,
            owner=owner,
            repo=repo,
            pr=parent_number,
            comment_id=comment_id,
            env=env,
        )
    else:  # PR_TOP_LEVEL
        _populate_issue_or_top_level_comment(
            cache_dir, repo_full=repo_full, comment_id=comment_id, env=env
        )
