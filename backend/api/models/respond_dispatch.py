"""Shared schemas for the respond workflow dispatch event.

The respond workflow dispatches with just an identifier — the target
comment URL. The CLI re-fetches the comment from the platform API so
user-supplied text (mention body, author) never crosses the backend →
container boundary.

Surface (PR top-level / inline thread / review submission / issue) is
recoverable from the URL itself: GitHub encodes it in the fragment
(``#discussion_r…`` etc.) and GitLab requires a tiny API call to inspect
the note's ``position``. Either way it isn't the dispatcher's concern.
"""

from __future__ import annotations

from pydantic import BaseModel


class RespondDispatchPayload(BaseModel):
    """Dispatch payload published on the provider's ``manual_dispatch`` stream
    when a respond run is queued.

    ``target_url`` is the comment URL the CLI will act on. Either
    ``pull_request_id`` or ``issue_id`` is set (used to attach the
    Execution row to the right entity); never both.
    """

    workflow: str = "respond"
    execution_id: str
    organization_id: str
    target_url: str
    pull_request_id: str | None = None
    issue_id: str | None = None
    # True when a respond execution was already active for this PR/issue at
    # reserve time: the row is created QUEUED but not published here — it
    # waits for api.plugins.container.respond_queue.dispatch_next_queued_respond
    # to publish it once that one finishes.
    queued_behind_active: bool = False


__all__ = ["RespondDispatchPayload"]
