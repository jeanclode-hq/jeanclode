from __future__ import annotations

import re
from pathlib import Path

from src.activities.decorator import activity
from src.activities.review.schemas import RouteDecision
from src.runtime.context import RunContext

_MAX_DIFF_CHARS = 800_000
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")
_NOISE_FILE_RE = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)
_NOISE_PATTERNS = (
    re.compile(
        r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
        r"uv\.lock|Cargo\.lock|go\.sum)$"
    ),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.(png|jpg|jpeg|gif|svg|ico|woff2?|ttf|otf|eot|pdf)$"),
)


def _all_files_are_noise(diff: str) -> bool:
    files = _NOISE_FILE_RE.findall(diff)
    if not files:
        return False
    return all(any(p.search(f) for p in _NOISE_PATTERNS) for f in files)


def _decide(diff: str) -> RouteDecision:
    if not diff.strip():
        return RouteDecision(action="stop", reason="empty diff")
    if len(diff) > _MAX_DIFF_CHARS:
        return RouteDecision(
            action="stop",
            reason=f"diff too large ({len(diff)} chars > {_MAX_DIFF_CHARS})",
        )
    if any(marker in diff for marker in _BINARY_MARKERS) and _all_files_are_noise(diff):
        return RouteDecision(action="stop", reason="binary-only changes")
    if _all_files_are_noise(diff):
        return RouteDecision(action="stop", reason="lockfile/asset-only changes")
    return RouteDecision(action="review", reason="diff is reviewable")


@activity(name="Filtering and routing")
def filter_and_route(*, ctx: RunContext) -> RouteDecision:
    diff_path = Path(ctx.cwd) / ".context" / "diff"
    if not diff_path.is_file():
        return RouteDecision(action="stop", reason="missing .context/diff")
    return _decide(diff_path.read_text())
