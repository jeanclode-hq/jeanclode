"""GitHub organization membership webhook handlers."""

import logging

from sqlalchemy.orm import Session

from api.database import (
    db_delete_org_membership,
    db_ensure_org_membership,
    db_ensure_workspace_membership,
    db_get_or_create_provider_identity,
    db_get_org_by_external_id,
    db_revoke_workspace_membership_if_no_orgs,
)

from ..utils import WebhookResponse
from .schemas import GitHubOrganizationMembershipEvent

logger = logging.getLogger(__name__)

_GITHUB_ROLE_MAP = {
    "admin": "admin",
    "owner": "owner",
}


def handle_member_added(
    event: GitHubOrganizationMembershipEvent,
    db: Session,
) -> WebhookResponse:
    """Handle organization.member_added — upsert OrgMembership and WorkspaceMembership."""
    org_external_id = str(event.organization.get("id", ""))
    user = event.membership.get("user", {})
    user_external_id = str(user.get("id", ""))
    username = user.get("login")
    avatar_url = user.get("avatar_url")
    role = _GITHUB_ROLE_MAP.get(event.membership.get("role", ""), "member")

    if not org_external_id or not user_external_id:
        return WebhookResponse(message="Missing org or user ID in payload", processed=False)

    org = db_get_org_by_external_id(db, org_external_id, provider="github")
    if not org:
        logger.debug(f"GitHub org {org_external_id} not found — skipping member_added")
        return WebhookResponse(message="Organization not found", processed=False)

    identity, _ = db_get_or_create_provider_identity(
        db=db,
        provider="github",
        external_id=user_external_id,
        username=username,
        avatar_url=avatar_url,
    )

    db_ensure_org_membership(db, org_id=org.id, provider_identity_id=identity.id, role=role)

    if org.workspace_id:
        db_ensure_workspace_membership(
            db, workspace_id=org.workspace_id, provider_identity_id=identity.id
        )

    logger.info(f"GitHub member_added: user {user_external_id} → org {org_external_id} ({role})")
    return WebhookResponse(message=f"Member {username} added to org {org.name}", processed=True)


def handle_member_removed(
    event: GitHubOrganizationMembershipEvent,
    db: Session,
) -> WebhookResponse:
    """Handle organization.member_removed — delete OrgMembership."""
    org_external_id = str(event.organization.get("id", ""))
    user = event.membership.get("user", {})
    user_external_id = str(user.get("id", ""))
    username = user.get("login")

    if not org_external_id or not user_external_id:
        return WebhookResponse(message="Missing org or user ID in payload", processed=False)

    org = db_get_org_by_external_id(db, org_external_id, provider="github")
    if not org:
        logger.debug(f"GitHub org {org_external_id} not found — skipping member_removed")
        return WebhookResponse(message="Organization not found", processed=False)

    from api.database.identity import db_get_identity_by_external_id

    identity = db_get_identity_by_external_id(db, provider="github", external_id=user_external_id)
    if not identity:
        return WebhookResponse(message="Identity not found — nothing to remove", processed=True)

    deleted = db_delete_org_membership(db, org_id=org.id, provider_identity_id=identity.id)

    if deleted and org.workspace_id and identity.user_id:
        db_revoke_workspace_membership_if_no_orgs(
            db, workspace_id=org.workspace_id, user_id=identity.user_id
        )

    logger.info(f"GitHub member_removed: user {user_external_id} ← org {org_external_id}")
    return WebhookResponse(
        message=f"Member {username} removed from org {org.name}"
        if deleted
        else "Membership not found",
        processed=True,
    )
