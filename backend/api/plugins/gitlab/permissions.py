"""Project member access-level checks against the GitLab API.

GitLab access levels:
- 10 Guest
- 20 Reporter
- 30 Developer
- 40 Maintainer
- 50 Owner

The respond workflow accepts Developer or higher — symmetric to the
GitHub ``write/triage`` threshold.
"""

from __future__ import annotations

import logging
from uuid import UUID

from api.context import get_current_app
from api.database.organization import db_get_org_by_id
from api.database.repository import db_get_repository_by_org_and_external_id

logger = logging.getLogger(__name__)


# Minimum access level required to dispatch the respond workflow.
DEVELOPER_LEVEL = 30


async def fetch_member_access_level(
    *,
    org_id: UUID,
    project_id: str,
    user_id: int,
) -> int:
    """Return the GitLab access level of ``user_id`` on ``project_id``.

    Token resolution order: org-level token first, then repo-level token as
    fallback (covers the case where tokens are stored per-repo rather than
    per-group).

    Returns ``0`` (= no access / unauthenticated lookup failed) on any
    non-200 response. Treats network errors the same — fail-closed.
    """
    app = get_current_app()
    db_plugin = app.database
    gitlab_plugin = app.gitlab
    if not db_plugin or not gitlab_plugin:
        return 0

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            return 0

        encrypted_token = org.auth_token_encrypted
        base_url = org.base_url or gitlab_plugin.get_effective_instance_url()

        if not encrypted_token:
            repo = db_get_repository_by_org_and_external_id(db, org_id, project_id)
            if repo and repo.auth_token_encrypted:
                encrypted_token = repo.auth_token_encrypted
                if repo.provider_url:
                    base_url = repo.provider_url

        if not encrypted_token:
            return 0

        try:
            token = db_plugin.decrypt(encrypted_token)
        except Exception:
            logger.exception(
                "Failed to decrypt GitLab token for org %s / project %s", org_id, project_id
            )
            return 0

    url = f"{base_url.rstrip('/')}/api/v4/projects/{project_id}/members/all/{user_id}"
    headers = {"PRIVATE-TOKEN": token}
    try:
        response = await gitlab_plugin.http.get(url, headers=headers)
    except Exception:
        logger.exception("gitlab member lookup failed: project=%s user=%s", project_id, user_id)
        return 0
    if response.status_code != 200:
        logger.info(
            "gitlab member lookup returned %s for project=%s user=%s",
            response.status_code,
            project_id,
            user_id,
        )
        return 0
    data = response.json()
    return int(data.get("access_level") or 0)


def is_authorized(level: int) -> bool:
    """Whether a GitLab access level may dispatch the respond workflow."""
    return level >= DEVELOPER_LEVEL
