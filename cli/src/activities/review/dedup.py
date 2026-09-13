from __future__ import annotations

from src.activities.decorator import activity
from src.activities.review.schemas import Comment
from src.runtime.context import RunContext


@activity(name="Filtering findings")
def dedup_against_existing_comments(
    comments: list[Comment],
    keep_indices: list[int] | None,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> list[Comment]:
    if keep_indices is None:
        return list(comments)
    valid = sorted({i for i in keep_indices if 0 <= i < len(comments)})
    return [comments[i] for i in valid]
