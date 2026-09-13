"""Tests for user database helpers — specifically first-login workspace membership linking."""

import uuid

from sqlalchemy.orm import Session

from api.database.user import db_create_or_update_user
from api.models.identities import ProviderIdentity
from api.models.organizations import Organization, OrgMembership
from api.models.workspaces import Workspace, WorkspaceMembership


def _create_workspace(db: Session, name: str = "test-workspace") -> Workspace:
    ws = Workspace(name=name, slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    return ws


def _create_org(
    db: Session, workspace: Workspace | None = None, provider: str = "github"
) -> Organization:
    org = Organization(
        workspace_id=workspace.id if workspace else None,
        name="test-org",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider=provider,
    )
    db.add(org)
    db.flush()
    return org


def _create_identity_with_membership(
    db: Session,
    org: Organization,
    role: str = "member",
) -> ProviderIdentity:
    """Create a ProviderIdentity (no user_id) and an OrgMembership for it."""
    identity = ProviderIdentity(
        provider="github",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        username=f"user-{uuid.uuid4().hex[:6]}",
        user_id=None,
    )
    db.add(identity)
    db.flush()

    membership = OrgMembership(
        org_id=org.id,
        provider_identity_id=identity.id,
        role=role,
    )
    db.add(membership)
    db.commit()
    db.refresh(identity)
    return identity


def test_first_login_creates_workspace_membership(db_session: Session) -> None:
    """When a user logs in for the first time, pre-existing OrgMembership rows should
    result in WorkspaceMembership rows being created for them."""
    ws = _create_workspace(db_session)
    org = _create_org(db_session, workspace=ws)
    identity = _create_identity_with_membership(db_session, org, role="member")
    db_session.commit()

    user, is_new = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="newuser@example.com",
        username=identity.username,
    )

    assert is_new is True

    ws_membership = (
        db_session.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == ws.id,
            WorkspaceMembership.user_id == user.id,
        )
        .first()
    )
    assert ws_membership is not None


def test_first_login_sets_last_workspace_id(db_session: Session) -> None:
    """last_workspace_id should be set to the first workspace found on first login."""
    ws = _create_workspace(db_session)
    org = _create_org(db_session, workspace=ws)
    identity = _create_identity_with_membership(db_session, org)
    db_session.commit()

    user, _ = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="newuser2@example.com",
        username=identity.username,
    )

    assert user.last_workspace_id == ws.id


def test_first_login_org_without_workspace_skipped(db_session: Session) -> None:
    """OrgMembership rows where the org has no workspace_id should not create memberships."""
    org = _create_org(db_session, workspace=None)
    identity = _create_identity_with_membership(db_session, org)
    db_session.commit()

    user, _ = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="noworkspace@example.com",
        username=identity.username,
    )

    count = (
        db_session.query(WorkspaceMembership).filter(WorkspaceMembership.user_id == user.id).count()
    )
    assert count == 0
    assert user.last_workspace_id is None


def test_returning_login_does_not_duplicate_memberships(db_session: Session) -> None:
    """A returning user logging in again should not create duplicate WorkspaceMembership rows."""
    ws = _create_workspace(db_session)
    org = _create_org(db_session, workspace=ws)
    identity = _create_identity_with_membership(db_session, org)
    db_session.commit()

    # First login — creates user and membership
    user, _ = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="returning@example.com",
        username=identity.username,
    )

    # Second login — identity already linked, no duplication expected
    user, _ = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="returning@example.com",
        username=identity.username,
    )

    count = (
        db_session.query(WorkspaceMembership).filter(WorkspaceMembership.user_id == user.id).count()
    )
    assert count == 1


def test_first_login_multiple_orgs_first_workspace_wins(db_session: Session) -> None:
    """With multiple orgs, last_workspace_id is set to the first workspace encountered."""
    ws1 = _create_workspace(db_session, "ws-alpha")
    ws2 = _create_workspace(db_session, "ws-beta")
    org1 = _create_org(db_session, workspace=ws1)
    org2 = _create_org(db_session, workspace=ws2)

    identity = ProviderIdentity(
        provider="github",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        username="multiorg-user",
        user_id=None,
    )
    db_session.add(identity)
    db_session.flush()

    db_session.add(OrgMembership(org_id=org1.id, provider_identity_id=identity.id, role="member"))
    db_session.add(OrgMembership(org_id=org2.id, provider_identity_id=identity.id, role="admin"))
    db_session.commit()
    db_session.refresh(identity)

    user, _ = db_create_or_update_user(
        db=db_session,
        provider="github",
        external_id=identity.external_id,
        email="multiorg@example.com",
        username=identity.username,
    )

    # Both workspace memberships should be created
    count = (
        db_session.query(WorkspaceMembership).filter(WorkspaceMembership.user_id == user.id).count()
    )
    assert count == 2

    # last_workspace_id is set (to one of the two workspaces)
    assert user.last_workspace_id in (ws1.id, ws2.id)


def test_first_login_multiple_orgs_same_workspace(db_session: Session) -> None:
    """Several orgs in one workspace must yield a single WorkspaceMembership, not one per org."""
    ws = _create_workspace(db_session, "ws-shared")
    orgs = [_create_org(db_session, workspace=ws) for _ in range(3)]

    identity = ProviderIdentity(
        provider="gitlab",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        username="shared-ws-user",
        user_id=None,
    )
    db_session.add(identity)
    db_session.flush()

    for org, role in zip(orgs, ("owner", "member", "owner"), strict=True):
        db_session.add(OrgMembership(org_id=org.id, provider_identity_id=identity.id, role=role))
    db_session.commit()
    db_session.refresh(identity)

    user, _ = db_create_or_update_user(
        db=db_session,
        provider="gitlab",
        external_id=identity.external_id,
        email="sharedws@example.com",
        username=identity.username,
    )

    count = (
        db_session.query(WorkspaceMembership).filter(WorkspaceMembership.user_id == user.id).count()
    )
    assert count == 1
    assert user.last_workspace_id == ws.id
