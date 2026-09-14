"""Helpers shared by the pr-summary workflow."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from src.activities.summary.schemas import FileLine, PostResult, PRSnapshot, SummaryPayload
from src.adaptors.diffn import DiffFile
from src.agents.schemas import AgentResult
from src.agents.summary.schemas import SummaryOutput

logger = logging.getLogger(__name__)


def load_pr_snapshot(repo_dir: Path) -> PRSnapshot | None:
    """Load the PR/MR context the adaptor preflight wrote under .context/.

    Returns None when required files are missing — callers should treat
    that as a fatal pre-flight error.
    """
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
    return PRSnapshot(
        platform=cast("Any", platform),
        repo=repo,
        pr=pr,
        pr_url=_read("pr_url"),
        pr_description=_read("pr_description", strip=False),
        diff=_read("diff", strip=False),
    )


def parse_description(result: AgentResult) -> str:
    """Pull a ``description`` field out of an agent result.

    Prefers ``result.structured`` (set when output_schema enforcement is
    on); falls back to parsing the surrounding text as JSON when the
    schema run didn't populate it.

    Defensively unwraps nested envelopes: models occasionally produce
    ``{"description": "{\\"description\\": \\"- bullet\\"}"}`` when
    their prompt's example invites preservation of the input shape.
    Without recursive unwrap the rendered summary contains the literal
    JSON envelope (real bug seen on PR #117 comment 4416689256).
    """
    raw = result.structured if result.structured is not None else _extract_json_object(result.text)
    text = (result.text or "").strip()
    return _normalize_escaped_newlines(_unwrap_description(raw, fallback=text))


def render_file_list(files: list[DiffFile]) -> str:
    """The ``<files>`` block the file summarizer reads."""
    rows = []
    for f in files:
        name = f"{f.old_path} -> {f.path}" if f.old_path else f.path
        noise = ", noise" if f.noise else ""
        rows.append(f"- {name} ({f.status}, +{f.additions} -{f.deletions}{noise})")
    return "\n".join(rows)


def build_file_lines(files: list[DiffFile], result: AgentResult) -> list[FileLine]:
    """Pair each diff file with its summary, in diff order.

    The file list comes from the diff, never from the model: a path the
    agent invents is dropped, and a file it skipped keeps an empty summary.
    No usable summary at all means no dropdown.
    """
    raw = result.structured if result.structured is not None else _extract_json_object(result.text)
    try:
        output = SummaryOutput.model_validate(raw or {})
    except ValueError:
        logger.warning("file summarizer returned an invalid payload")
        output = SummaryOutput()
    summaries = {s.path: s.summary for s in output.files if s.summary.strip()}
    if not summaries.keys() & {f.path for f in files}:
        return []
    return [
        FileLine(path=f.path, old_path=f.old_path, summary=summaries.get(f.path, "")) for f in files
    ]


def _normalize_escaped_newlines(text: str) -> str:
    """Collapse literal backslash-n sequences left over from over-escaped JSON.

    Models occasionally double-escape newlines inside the JSON payload
    (``"\\\\n"`` instead of ``"\\n"``), which ``json.loads`` faithfully
    decodes to a literal backslash followed by ``n`` rather than an actual
    newline. Left uncorrected, the platform renders the whole description
    as a single bullet with a stray ``\\n-`` printed as text.
    """
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def _unwrap_description(value: object, *, fallback: str, depth: int = 0) -> str:
    """Walk a possibly-nested ``{"description": ...}`` envelope to the leaf string."""
    if depth > 5:
        return fallback  # guardrail against pathological recursion
    if isinstance(value, dict):
        inner = value.get("description")
        if isinstance(inner, str):
            stripped = inner.strip()
            if not stripped:
                return fallback
            nested = _extract_json_object(stripped)
            if isinstance(nested, dict) and "description" in nested:
                return _unwrap_description(nested, fallback=stripped, depth=depth + 1)
            return stripped
    return fallback


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parse of an agent's ``result`` text into a JSON object.

    Tolerates ```json fences and prose surrounding the object — exactly
    what code-review's utils does, kept local so the two workflows stay
    independently evolvable.
    """
    if not text:
        return None
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


def panel_title(post: PostResult) -> str:
    if post.posted:
        return "Description Updated"
    return "Description Update Failed"


def panel_style(post: PostResult) -> str:
    return "green" if post.posted else "red"


def result_panel(post: PostResult, payload: SummaryPayload, snap: PRSnapshot) -> str:
    """Build the post-run panel content shown in the TTY UI."""
    url = post.pr_url or snap.pr_url
    if not post.posted:
        return post.error or "update-pr-description returned a non-zero exit code"
    header = f"[bold]{url}[/]" if url else ""
    return f"{header}\n\n{payload.body}" if header else payload.body
