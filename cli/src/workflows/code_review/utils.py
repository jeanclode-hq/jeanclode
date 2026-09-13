from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from src.activities.review.schemas import (
    Comment,
    GitHubComment,
    GitHubReview,
    GitLabComment,
    GitLabReview,
    PostResult,
    PRContext,
    PRRef,
    RecoverResult,
)
from src.agents.schemas import AgentResult

logger = logging.getLogger(__name__)


def load_pr_context(repo_dir: Path) -> PRContext | None:
    cache = repo_dir / ".context"
    if not cache.is_dir():
        return None

    def _read(name: str, *, strip: bool = True) -> str:
        path = cache / name
        if not path.is_file():
            return ""
        text = path.read_text()
        return text.strip() if strip else text

    platform = _read("platform")
    repo = _read("repo")
    pr = _read("pr")
    if platform not in ("github", "gitlab") or not repo or not pr:
        return None
    ref = PRRef(
        platform=cast("Any", platform),
        repo=repo,
        pr=pr,
        pr_url=_read("pr_url"),
        pr_author=_read("pr_author"),
    )
    return PRContext(
        ref=ref,
        pr_description=_read("pr_description", strip=False),
        diff=_read("diff", strip=False),
        discussions=_read("discussions", strip=False),
    )


def parse_review(result: AgentResult, platform: str) -> list[Comment]:
    if platform == "github":
        review_cls: type[GitHubReview] | type[GitLabReview] = GitHubReview
        comment_cls: type[GitHubComment] | type[GitLabComment] = GitHubComment
    else:
        review_cls = GitLabReview
        comment_cls = GitLabComment

    if result.structured is not None:
        try:
            review = review_cls.model_validate(result.structured)
            return list(review.comments)
        except Exception as exc:
            logger.warning("structured review parse failed: %s", exc)

    raw = _extract_json_object(result.text)
    if isinstance(raw, dict) and "comments" in raw:
        items = raw["comments"]
        if isinstance(items, list):
            comments: list[Comment] = []
            for item in items:
                try:
                    comments.append(comment_cls.model_validate(item))
                except Exception as exc:
                    logger.warning("skipping invalid comment: %s", exc)
            return comments
    return []


def parse_keep_indices(result: AgentResult) -> list[int] | None:
    raw = result.structured if result.structured is not None else _extract_json_object(result.text)
    if not isinstance(raw, dict):
        return None
    indices = raw.get("keep_indices")
    if not isinstance(indices, list):
        return None
    return [i for i in indices if isinstance(i, int)]


def parse_styled_bodies(result: AgentResult) -> list[str] | None:
    raw = result.structured if result.structured is not None else _extract_json_object(result.text)
    if not isinstance(raw, dict):
        return None
    bodies = raw.get("bodies")
    if not isinstance(bodies, list):
        return None
    return [b for b in bodies if isinstance(b, str)]


def serialize_comments(comments: list[Comment]) -> str:
    return json.dumps({"comments": [c.model_dump() for c in comments]})


def serialize_numbered(comments: list[Comment]) -> str:
    items: list[dict[str, Any]] = []
    for i, c in enumerate(comments):
        d = c.model_dump()
        d["index"] = i
        items.append(d)
    return json.dumps(items)


def panel_title(post: PostResult, recover: RecoverResult | None = None) -> str:
    if post.lgtm:
        return "LGTM"
    if recover and recover.error:
        return "Review Posted (with errors)"
    return "Review Posted"


def panel_style(post: PostResult, recover: RecoverResult | None = None) -> str:
    truly_failed = recover.error if recover else bool(post.failed)
    if truly_failed and post.posted == 0:
        return "red"
    if truly_failed:
        return "yellow"
    return "green"


def result_panel(post: PostResult, recover: RecoverResult | None, pr_ref: PRRef) -> str:
    url = post.pr_url or pr_ref.pr_url or _fallback_url(pr_ref)
    lines: list[str] = [f"[bold]{url}[/]", ""]
    if post.lgtm:
        lines.append("[green]LGTM 👍[/] — no findings to post.")
        return "\n".join(lines)

    bits: list[str] = []
    if post.posted:
        bits.append(f"[green]{post.posted} posted inline[/]")
    if recover and recover.recovered:
        bits.append(f"[green]{recover.recovered} posted top-level[/]")
    if recover and recover.error:
        bits.append(f"[red]{len(post.unposted)} could not be posted[/]")
    if bits:
        lines.append(" · ".join(bits))

    if recover and recover.error:
        lines.append("")
        lines.append(f"[red]top-level fallback failed: {recover.error}[/]")
        lines.append("")
        lines.append("[bold]Unposted:[/]")
        for u in post.unposted:
            anchor = f"{u.path}:{u.line}" if u.path and u.line else (u.path or "(no anchor)")
            lines.append(f"  [dim]{u.error_type}[/] {anchor}")
    return "\n".join(lines).rstrip()


def dry_run_panel(comments: list[Comment], pr_ref: PRRef) -> str:
    url = pr_ref.pr_url or _fallback_url(pr_ref)
    if not comments:
        return f"[bold]{url}[/]\n\nNo findings — would post LGTM."
    lines = [f"[bold]{url}[/]", "", f"Would post [bold]{len(comments)}[/] inline comment(s):", ""]
    for i, c in enumerate(comments, 1):
        anchor = comment_anchor(c)
        first_line = c.body.splitlines()[0] if c.body else ""
        lines.append(f"  {i}. [cyan]{anchor}[/] {first_line}")
    return "\n".join(lines)


def _fallback_url(pr_ref: PRRef) -> str:
    if pr_ref.platform == "github":
        return f"https://github.com/{pr_ref.repo}/pull/{pr_ref.pr}"
    return f"https://gitlab.com/{pr_ref.repo}/-/merge_requests/{pr_ref.pr}"


def comment_anchor(c: Comment) -> str:
    if isinstance(c, GitHubComment):
        return f"{c.path}:{c.line}" if c.line else c.path
    path = c.new_path or c.old_path or "?"
    line = c.new_line or c.old_line
    return f"{path}:{line}" if line else path


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
