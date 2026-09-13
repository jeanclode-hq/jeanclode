"""Instance-wide GitLab system hooks (Admin Area → System Hooks).

Every system hook delivery carries ``X-Gitlab-Event: System Hook``, and most of
its payloads are ``event_name``-shaped with no ``object_kind`` — the same shape
as the group-level hooks, so the same handlers apply.

What differs is trust and reach. A system hook fires for *every* project and
group on the instance, the vast majority of which nobody here connected, so a
project only becomes a repo when ``projects.ensure_project`` can prove a
connected group owns it. It is also the only hook an admin can set once for the
whole instance, instead of per group.

Its other events — ``user_create``, ``key_create``, ``push``, ``tag_push``,
``repository_update`` and so on — are either covered elsewhere (a system-hook
``merge_request`` payload carries ``object_kind`` and is routed like any other
MR event) or are not ours.
"""

import logging
from typing import Any

from api.database import db_delete_org_by_id, db_get_org_by_external_id

from ..utils import WebhookResponse
from . import projects
from .group_hook import MEMBER_EVENTS

logger = logging.getLogger(__name__)

# Group lifecycle, as a system hook reports it. ``group_destroy`` has no
# group-webhook equivalent — a group cannot announce its own deletion — so this
# is the only way to learn that a connected group is gone.
GROUP_EVENTS = frozenset({"group_destroy"})

SYSTEM_HOOK_EVENTS = projects.PROJECT_EVENTS | GROUP_EVENTS | MEMBER_EVENTS


async def handle_system_hook_event(
    event: dict[str, Any],
    db_plugin: Any,
    gitlab_plugin: Any,
) -> WebhookResponse:
    """Act on an instance-wide system hook event."""
    payload = event.get("payload") or {}
    instance_url = event.get("instance_url")
    event_name = payload.get("event_name", "")

    if event_name in projects.ENSURE_EVENTS:
        if not payload.get("project_id"):
            return WebhookResponse(message="Missing project_id in payload", processed=False)
        return await projects.ensure_project(payload, instance_url, db_plugin, gitlab_plugin)

    if event_name in projects.REMOVE_EVENTS:
        if not payload.get("project_id"):
            return WebhookResponse(message="Missing project_id in payload", processed=False)
        return projects.remove_project(payload, db_plugin)

    if event_name in GROUP_EVENTS:
        return _remove_group(payload, instance_url, db_plugin)

    return WebhookResponse(
        message=f"System hook event '{event_name}' not processed", processed=False
    )


def _remove_group(
    payload: dict[str, Any],
    instance_url: str | None,
    db_plugin: Any,
) -> WebhookResponse:
    """Delete the org row for a deleted group, and its repos with it."""
    group_id = str(payload.get("group_id", ""))
    full_path = payload.get("full_path") or payload.get("path", group_id)

    if not group_id:
        return WebhookResponse(message="Missing group_id in payload", processed=False)

    with db_plugin.session() as db:
        org = db_get_org_by_external_id(db, group_id, provider="gitlab")
        if not org or not projects.same_instance(org.base_url, instance_url):
            return WebhookResponse(message=f"Group {full_path} not tracked", processed=True)
        db_delete_org_by_id(db, org.id)

    logger.info(f"Removed GitLab group org {full_path} ({group_id})")
    return WebhookResponse(message=f"Removed group {full_path}", processed=True)
