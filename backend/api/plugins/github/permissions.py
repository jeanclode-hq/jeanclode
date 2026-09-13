"""Collaborator permission checks against the GitHub API.

The mention-driven respond workflow gates dispatch on the commenter's
write/triage access to the repository — a Reporter-level user who happens
to comment on a PR shouldn't be able to drive code changes.
"""

from __future__ import annotations

import logging
from typing import Literal

from api.context import get_current_app

logger = logging.getLogger(__name__)


PermissionLevel = Literal["none", "read", "triage", "write", "maintain", "admin"]

# Permissions that may dispatch the respond workflow. Read/none are
# rejected so a generic external commenter cannot trigger the loop.
_AUTHORIZED_LEVELS = frozenset({"triage", "write", "maintain", "admin"})


async def fetch_collaborator_permission(
    *,
    installation_token: str,
    owner: str,
    repo: str,
    username: str,
) -> PermissionLevel:
    """Return the GitHub collaborator permission for ``username`` on ``owner/repo``.

    Falls back to ``"none"`` on any non-200 response (including 404, which GitHub
    returns for users that are not collaborators). Treats network errors the
    same as ``"none"`` — fail-closed for authorization.
    """
    app = get_current_app()
    if not app.github:
        logger.warning("github plugin not loaded; denying permission check")
        return "none"

    url = f"/repos/{owner}/{repo}/collaborators/{username}/permission"
    headers = {
        "Authorization": f"token {installation_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = await app.github.http.get(url, headers=headers)
    except Exception:
        logger.exception(
            "collaborator permission check failed: %s/%s user=%s", owner, repo, username
        )
        return "none"

    if response.status_code != 200:
        logger.info(
            "collaborator permission lookup returned %s for %s/%s user=%s",
            response.status_code,
            owner,
            repo,
            username,
        )
        return "none"

    data = response.json()
    permission = (data.get("permission") or "").lower()
    if permission in {"none", "read", "triage", "write", "maintain", "admin"}:
        return permission  # type: ignore[return-value]
    return "none"


def is_authorized(level: PermissionLevel) -> bool:
    """Whether a permission level may dispatch the respond workflow."""
    return level in _AUTHORIZED_LEVELS
