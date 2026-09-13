"""GitLab webhook endpoint (hot path — validate and queue).

GitLab sends two payload shapes down the same URL. Project- and group-scoped
activity (merge requests, issues, notes) carries ``object_kind``; the
group-specific and instance-wide lifecycle events (project, subgroup, member,
system) carry ``event_name`` and no ``object_kind`` at all. Dispatching on
``object_kind`` alone silently drops every event of the second kind, so the
shape is checked first and the ``X-Gitlab-Event`` header decides how far to
trust it — ``System Hook`` reaches the whole instance, a group hook only its
own group.
"""

import json
import logging

from fastapi import APIRouter, Depends, Header

from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker

from ..utils import WebhookResponse
from .dependency import verify_gitlab_webhook
from .group_hook import GROUP_HOOK_EVENTS, MEMBER_EVENTS
from .system_hook import SYSTEM_HOOK_EVENTS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gitlab", tags=["Webhooks", "GitLab"])

SYSTEM_HOOK_HEADER = "System Hook"


async def _queue(stream: str, message: object) -> None:
    """Publish to a Redis Stream with the shared retention cap."""
    broker = get_faststream_broker()
    await broker.publish(message, stream=stream, maxlen=STREAM_MAXLEN)


async def _queue_lifecycle_event(
    payload: dict,
    event_name: str,
    instance_url: str | None,
    is_system_hook: bool,
) -> WebhookResponse:
    """Route an ``event_name``-shaped payload: member, project, subgroup, group."""
    # Member events have their own consumer and are the same shape whichever
    # hook sent them, so they take the same stream from either origin.
    if event_name in MEMBER_EVENTS:
        await _queue("jeanclode.events.gitlab.members", payload)
        return WebhookResponse(message=f"{event_name} event queued", processed=True)

    known = SYSTEM_HOOK_EVENTS if is_system_hook else GROUP_HOOK_EVENTS
    if event_name not in known:
        return WebhookResponse(
            message=f"Event '{event_name}' not supported",
            processed=False,
        )

    stream = "jeanclode.events.gitlab.system" if is_system_hook else "jeanclode.events.gitlab.group"
    await _queue(stream, {"instance_url": instance_url, "payload": payload})
    return WebhookResponse(message=f"{event_name} event queued", processed=True)


@router.post(
    "",
    operation_id="gitlab_webhook",
    response_model=WebhookResponse,
    status_code=202,
)
async def gitlab_webhook(
    body: bytes = Depends(verify_gitlab_webhook),
    x_gitlab_event: str | None = Header(
        None, description="GitLab hook type, e.g. 'Push Hook' or 'System Hook'"
    ),
    x_gitlab_instance: str | None = Header(
        None, description="GitLab instance URL the event came from"
    ),
) -> WebhookResponse:
    """Receive a GitLab webhook, verify token, queue supported events."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return WebhookResponse(message="Invalid JSON payload", processed=False)

    event_type = payload.get("object_kind", "")
    event_name = payload.get("event_name", "")

    # Lifecycle events first: they are the ones with no ``object_kind``. A
    # system hook's merge_request payload does carry one, and falls through to
    # the regular dispatch below like any other MR event.
    if event_name and not event_type:
        return await _queue_lifecycle_event(
            payload,
            event_name,
            x_gitlab_instance,
            is_system_hook=x_gitlab_event == SYSTEM_HOOK_HEADER,
        )

    if event_type == "merge_request":
        await _queue("jeanclode.events.gitlab.merge_requests", body)
        return WebhookResponse(message="merge_request event queued", processed=True)

    if event_type == "issue":
        await _queue("jeanclode.events.gitlab.issues", body)
        return WebhookResponse(message="issue event queued", processed=True)

    if event_type == "note":
        # ``note`` covers MR comments, MR inline diff replies, and Issue
        # comments — discriminated by ``noteable_type`` downstream.
        await _queue("jeanclode.events.gitlab.mentions", body)
        return WebhookResponse(message="note event queued", processed=True)

    if event_type == "member":
        # Documented member payloads carry ``event_name`` instead and are
        # handled above; this stays for any instance that sends both.
        await _queue("jeanclode.events.gitlab.members", body)
        return WebhookResponse(message="member event queued", processed=True)

    return WebhookResponse(
        message=f"Event type '{event_type}' not supported",
        processed=False,
    )
