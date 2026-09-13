from __future__ import annotations

from src.activities.decorator import activity
from src.activities.review.schemas import Comment
from src.runtime.context import RunContext


@activity(name="Applying styled comment bodies")
def style_comments(
    comments: list[Comment],
    bodies: list[str] | None,
    *,
    ctx: RunContext,  # noqa: ARG001
) -> list[Comment]:
    if bodies is None or len(bodies) != len(comments):
        return list(comments)
    styled: list[Comment] = []
    for c, body in zip(comments, bodies, strict=True):
        if body and body.strip():
            d = c.model_dump()
            d["body"] = body
            styled.append(type(c).model_validate(d))
        else:
            styled.append(c)
    return styled
