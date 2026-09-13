"""Database operations for User model."""

from uuid import UUID

from sqlalchemy.orm import Session

from api.database.identity import db_get_or_create_provider_identity
from api.models.organizations import OrgMembership
from api.models.users import User
from api.models.workspaces import WorkspaceMembership


def db_get_user_by_id(db: Session, user_id: UUID) -> User | None:
    """Get user by ID."""
    return db.query(User).filter(User.id == user_id).first()


def db_get_user_by_email(db: Session, email: str) -> User | None:
    """Get user by email."""
    return db.query(User).filter(User.email == email).first()


def _link_identity_org_memberships(
    db: Session,
    provider_identity_id: UUID,
    user: User,
) -> None:
    """Propagate pre-existing OrgMembership rows to WorkspaceMemberships for a newly linked user.

    When a ProviderIdentity is linked to a User for the first time, any OrgMembership rows
    that were created before the user existed (e.g. from a GitHub org webhook) need to be
    reflected as WorkspaceMembership rows so the user can access their workspace immediately.
    Also sets user.last_workspace_id to the first workspace found, if not already set.
    """
    org_memberships = (
        db.query(OrgMembership)
        .filter(OrgMembership.provider_identity_id == provider_identity_id)
        .all()
    )

    # Several orgs can share one workspace, so dedupe before inserting: the
    # session runs with autoflush off, and a per-iteration existence check
    # would not see rows added earlier in this loop.
    workspace_ids: list[UUID] = []
    for org_membership in org_memberships:
        workspace_id = org_membership.organization.workspace_id
        if workspace_id is not None and workspace_id not in workspace_ids:
            workspace_ids.append(workspace_id)

    if not workspace_ids:
        return

    already_member = {
        row[0]
        for row in db.query(WorkspaceMembership.workspace_id)
        .filter(
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.workspace_id.in_(workspace_ids),
        )
        .all()
    }

    for workspace_id in workspace_ids:
        if workspace_id not in already_member:
            db.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user.id))

        if user.last_workspace_id is None:
            user.last_workspace_id = workspace_id


def db_create_or_update_user(
    db: Session,
    provider: str,
    external_id: str,
    email: str,
    username: str,
    avatar_url: str | None = None,
    candidate_emails: list[str] | None = None,
) -> tuple[User, bool]:
    """Create or update user from OAuth data.

    Finds or creates a ProviderIdentity, then resolves the User:
    already linked → matched by any known email → new user.
    Links the identity if not already linked and propagates org memberships.
    """
    identity, _ = db_get_or_create_provider_identity(
        db=db,
        provider=provider,
        external_id=external_id,
        username=username,
        avatar_url=avatar_url,
    )

    user = None
    is_new_user = False

    if identity.user_id:
        user = db.query(User).filter(User.id == identity.user_id).first()

    if not user:
        for addr in candidate_emails or [email]:
            user = db_get_user_by_email(db, addr)
            if user:
                break

    if not user:
        user = User(email=email)
        db.add(user)
        db.flush()
        is_new_user = True

    if identity.user_id != user.id:
        identity.user_id = user.id
        _link_identity_org_memberships(db, provider_identity_id=identity.id, user=user)

    db.commit()
    db.refresh(user)
    db.refresh(identity)
    return user, is_new_user
