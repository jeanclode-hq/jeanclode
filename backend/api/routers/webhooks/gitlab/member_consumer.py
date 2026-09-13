"""FastStream consumer for GitLab member webhook events."""

import logging
from typing import Any

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_delete_org_membership,
    db_ensure_org_membership,
    db_ensure_workspace_membership,
    db_get_identity_by_external_id,
    db_get_or_create_provider_identity,
    db_get_org_by_external_id,
    db_get_org_by_id,
    db_get_repository_by_external_id,
    db_revoke_workspace_membership_if_no_orgs,
)

logger = logging.getLogger(__name__)

router = RedisRouter()

_ACCESS_LEVEL_TO_ROLE = {
    "owner": "owner",
    "maintainer": "admin",
    "developer": "member",
    "reporter": "member",
    "guest": "member",
}

# Group-level hooks use _to_group; project-level hooks use _to_team.
_ADD_OR_UPDATE = frozenset(
    [
        "user_add_to_group",
        "user_update_for_group",
        "user_add_to_team",
        "user_update_for_team",
    ]
)
_REMOVE = frozenset(["user_remove_from_group", "user_remove_from_team"])


def _map_access_level(group_access: str) -> str:
    return _ACCESS_LEVEL_TO_ROLE.get(group_access.lower(), "member")


@router.subscriber(
    stream=StreamSub("jeanclode.events.gitlab.members", group="jeanclode", consumer="worker-1")
)
async def consume_gitlab_member(event: dict[str, Any]) -> None:
    """Consume a GitLab member webhook from the Redis Stream."""
    app = get_current_app()
    if not app.database:
        logger.error("Database plugin not configured")
        return

    event_name = event.get("event_name", "")
    user_id = str(event.get("user_id", ""))

    if not user_id:
        logger.warning("Member event missing user_id")
        return

    if event_name not in _ADD_OR_UPDATE and event_name not in _REMOVE:
        logger.debug(f"Unhandled member event_name: {event_name}")
        return

    # Group hooks carry group_id; project hooks carry project_id.
    group_id = str(event.get("group_id", ""))
    project_id = str(event.get("project_id", ""))

    if not group_id and not project_id:
        logger.warning("Member event missing both group_id and project_id")
        return

    with app.database.session() as db:
        if group_id:
            org = db_get_org_by_external_id(db, group_id, provider="gitlab")
        else:
            repo = db_get_repository_by_external_id(db, project_id)
            org = db_get_org_by_id(db, repo.org_id) if repo else None

        if not org:
            logger.debug(
                f"No org found for GitLab {'group' if group_id else 'project'} "
                f"{group_id or project_id}, skipping member event"
            )
            return

        if event_name in _ADD_OR_UPDATE:
            username = event.get("user_username", "")
            role = _map_access_level(event.get("group_access", ""))
            identity, _ = db_get_or_create_provider_identity(
                db, provider="gitlab", external_id=user_id, username=username
            )
            membership = db_ensure_org_membership(db, org.id, identity.id, role=role)
            if membership.role != role:
                membership.role = role
                db.commit()
            if identity.user_id and org.workspace_id:
                db_ensure_workspace_membership(db, org.workspace_id, identity.id)
            logger.debug(f"Upserted GitLab member {username} ({user_id}) in org {org.id} as {role}")

        else:  # _REMOVE
            remove_identity = db_get_identity_by_external_id(
                db, provider="gitlab", external_id=user_id
            )
            if not remove_identity:
                logger.debug(f"No identity found for GitLab user {user_id}, nothing to remove")
                return
            db_delete_org_membership(db, org.id, remove_identity.id)
            if remove_identity.user_id and org.workspace_id:
                db_revoke_workspace_membership_if_no_orgs(
                    db, org.workspace_id, remove_identity.user_id
                )
            logger.debug(f"Removed GitLab member {user_id} from org {org.id}")
