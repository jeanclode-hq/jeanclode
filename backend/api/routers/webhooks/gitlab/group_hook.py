"""Group-level GitLab hooks — the events a group owner can configure.

A group webhook fires for everything inside the group and its subgroups. Three
of its event families are *group-specific* and share a payload shape that the
rest of GitLab's webhooks do not: they carry ``event_name`` and no
``object_kind``, which is why dispatching on ``object_kind`` alone silently
drops every one of them.

| Family   | ``X-Gitlab-Event`` | Events                                    |
|----------|--------------------|-------------------------------------------|
| Project  | ``Project Hook``   | project_create, project_destroy           |
| Subgroup | ``Subgroup Hook``  | subgroup_create, subgroup_destroy         |
| Member   | ``Member Hook``    | user_add_to_group, user_remove_from_group |

Member events keep their own consumer (``member_consumer``); the route sends
them straight there. This module owns the two that change *what exists*:
projects and subgroups.
"""

import logging
from typing import Any

from api.database import (
    db_delete_org_by_id,
    db_get_org_by_external_id,
)

from ..utils import WebhookResponse
from . import projects

logger = logging.getLogger(__name__)

# Group member events, handled by ``member_consumer`` on its own stream. Named
# here so the route can recognize them without importing that consumer.
MEMBER_EVENTS = frozenset(
    {
        "user_add_to_group",
        "user_update_for_group",
        "user_remove_from_group",
        "user_add_to_team",
        "user_update_for_team",
        "user_remove_from_team",
    }
)

SUBGROUP_EVENTS = frozenset({"subgroup_create", "subgroup_destroy"})

# What a group hook can tell us that we act on. Everything else it sends
# (push, pipeline, job, …) either arrives as an ``object_kind`` payload or is
# not ours.
GROUP_HOOK_EVENTS = projects.PROJECT_EVENTS | SUBGROUP_EVENTS | MEMBER_EVENTS


async def handle_group_hook_event(
    event: dict[str, Any],
    db_plugin: Any,
    gitlab_plugin: Any,
) -> WebhookResponse:
    """Act on a group-specific hook event (project or subgroup lifecycle)."""
    payload = event.get("payload") or {}
    instance_url = event.get("instance_url")
    event_name = payload.get("event_name", "")

    if event_name in projects.ENSURE_EVENTS:
        return await projects.ensure_project(payload, instance_url, db_plugin, gitlab_plugin)

    if event_name in projects.REMOVE_EVENTS:
        return projects.remove_project(payload, db_plugin)

    if event_name == "subgroup_destroy":
        return _remove_subgroup(payload, instance_url, db_plugin)

    if event_name == "subgroup_create":
        # Nothing to hold yet. The org row for a subgroup is created as a
        # placeholder when its first project shows up, which keeps empty
        # subgroups out of the dashboard.
        return WebhookResponse(message="Subgroup noted, nothing to track yet", processed=True)

    return WebhookResponse(
        message=f"Group hook event '{event_name}' not processed", processed=False
    )


def _remove_subgroup(
    payload: dict[str, Any],
    instance_url: str | None,
    db_plugin: Any,
) -> WebhookResponse:
    """Delete the org row for a deleted subgroup, and its repos with it."""
    group_id = str(payload.get("group_id", ""))
    full_path = payload.get("full_path", group_id)

    if not group_id:
        return WebhookResponse(message="Missing group_id in payload", processed=False)

    with db_plugin.session() as db:
        org = db_get_org_by_external_id(db, group_id, provider="gitlab")
        if not org or not projects.same_instance(org.base_url, instance_url):
            return WebhookResponse(message=f"Subgroup {full_path} not tracked", processed=True)
        db_delete_org_by_id(db, org.id)

    logger.info(f"Removed GitLab subgroup org {full_path} ({group_id})")
    return WebhookResponse(message=f"Removed subgroup {full_path}", processed=True)
