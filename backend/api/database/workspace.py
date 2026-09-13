"""Database operations for Workspace model."""

from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from api.models.identities import ProviderIdentity
from api.models.organizations import Organization, OrgMembership
from api.models.users import User
from api.models.workspaces import Workspace, WorkspaceMembership


def db_get_workspace_by_id(
    db: Session,
    workspace_id: UUID,
) -> Workspace | None:
    """Get workspace by ID."""
    return db.query(Workspace).filter(Workspace.id == workspace_id).first()


def db_get_workspace_by_slug(
    db: Session,
    slug: str,
) -> Workspace | None:
    """Get workspace by slug."""
    return db.query(Workspace).filter(Workspace.slug == slug).first()


def db_is_workspace_member(
    db: Session,
    workspace_id: UUID,
    user_id: UUID,
) -> bool:
    """Check if a user is a member of a workspace."""
    return (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
        .first()
        is not None
    )


def db_get_workspaces_by_user(
    db: Session,
    user_id: UUID,
) -> list[Workspace]:
    """Get all workspaces a user is a member of."""
    return (
        db.query(Workspace)
        .join(WorkspaceMembership)
        .filter(WorkspaceMembership.user_id == user_id)
        .all()
    )


def db_create_workspace(
    db: Session,
    name: str,
    slug: str,
) -> Workspace:
    """Create a new workspace."""
    workspace = Workspace(name=name, slug=slug)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def db_create_workspace_membership(
    db: Session,
    workspace_id: UUID,
    user_id: UUID,
) -> WorkspaceMembership:
    """Create a workspace membership."""
    membership = WorkspaceMembership(
        workspace_id=workspace_id,
        user_id=user_id,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership


def db_ensure_workspace_membership(
    db: Session,
    workspace_id: UUID,
    provider_identity_id: UUID,
) -> WorkspaceMembership | None:
    """Ensure a provider identity's user is a member of the workspace.

    Resolves provider_identity_id → user_id and creates a WorkspaceMembership
    if one doesn't already exist. Returns the membership, or None if the
    provider identity doesn't exist.
    """
    identity = (
        db.query(ProviderIdentity).filter(ProviderIdentity.id == provider_identity_id).first()
    )
    if not identity or identity.user_id is None:
        return None

    existing = (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == identity.user_id,
        )
        .first()
    )
    if existing:
        return existing

    membership = WorkspaceMembership(
        workspace_id=workspace_id,
        user_id=identity.user_id,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership


def db_get_workspace_members(
    db: Session,
    workspace_id: UUID,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[tuple[WorkspaceMembership, User]], int]:
    """Return paginated workspace members with their user records.

    Returns:
        Tuple of (list of (WorkspaceMembership, User) tuples, total count).
    """
    base_q = (
        db.query(WorkspaceMembership, User)
        .join(User, WorkspaceMembership.user_id == User.id)
        .options(selectinload(User.identities))
        .filter(WorkspaceMembership.workspace_id == workspace_id)
    )
    total = base_q.count()
    rows = base_q.offset((page - 1) * limit).limit(limit).all()
    return rows, total


def db_revoke_workspace_membership_if_no_orgs(
    db: Session,
    workspace_id: UUID,
    user_id: UUID,
) -> bool:
    """Remove WorkspaceMembership when a user has no remaining OrgMemberships in a workspace.

    Counts OrgMembership rows (across all provider identities of the user) for orgs belonging
    to the given workspace. If none remain, deletes the WorkspaceMembership.

    Returns True if the membership was revoked.
    """
    identity_ids = [
        row[0]
        for row in db.query(ProviderIdentity.id).filter(ProviderIdentity.user_id == user_id).all()
    ]
    if not identity_ids:
        return False

    remaining = (
        db.query(OrgMembership)
        .join(Organization, OrgMembership.org_id == Organization.id)
        .filter(
            OrgMembership.provider_identity_id.in_(identity_ids),
            Organization.workspace_id == workspace_id,
        )
        .count()
    )

    if remaining > 0:
        return False

    deleted = (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
        .delete()
    )
    db.commit()
    return deleted > 0
