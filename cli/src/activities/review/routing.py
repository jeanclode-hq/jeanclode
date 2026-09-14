from __future__ import annotations

from pathlib import Path

from src.activities.decorator import activity
from src.activities.review.schemas import RouteDecision
from src.adaptors.diffn import all_files_are_noise
from src.runtime.context import RunContext

_MAX_DIFF_CHARS = 800_000


def _decide(diff: str) -> RouteDecision:
    if not diff.strip():
        return RouteDecision(action="stop", reason="empty diff")
    if len(diff) > _MAX_DIFF_CHARS:
        return RouteDecision(
            action="stop",
            reason=f"diff too large ({len(diff)} chars > {_MAX_DIFF_CHARS})",
        )
    if all_files_are_noise(diff):
        return RouteDecision(action="stop", reason="lockfile/asset-only changes")
    return RouteDecision(action="review", reason="diff is reviewable")


@activity(name="Filtering and routing")
def filter_and_route(*, ctx: RunContext) -> RouteDecision:
    diff_path = Path(ctx.cwd) / ".context" / "diff"
    if not diff_path.is_file():
        return RouteDecision(action="stop", reason="missing .context/diff")
    return _decide(diff_path.read_text())
