"""Tests for GitHub organization membership webhook handlers."""

import uuid

import pytest
from sqlalchemy.orm import Session

from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.organizations import Organization, OrgMembership
from api.models.users import User
from api.models.workspaces import Workspace, WorkspaceMembership
from api.routers.webhooks.github.memberships import handle_member_added, handle_member_removed
from api.routers.webhooks.github.schemas import GitHubOrganizationMembershipEvent


@pytest.fixture
def db(app):
    """Create tables and yield a database session."""
    Base.metadata.create_all(bind=app.database.engine)
    session = app.database.get_session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=app.database.engine)


def _make_event(
    action: str, org_id: str, user_id: str, username: str, role: str = "member"
) -> GitHubOrganizationMembershipEvent:
    return GitHubOrganizationMembershipEvent(
        action=action,
        organization={"id": org_id, "login": "test-org"},
        membership={
            "user": {"id": user_id, "login": username, "avatar_url": None},
            "role": role,
        },
        sender={"id": 1, "login": "admin"},
    )


def _setup_org_with_workspace(
    db: Session, provider: str = "github"
) -> tuple[Workspace, Organization]:
    ws = Workspace(name="test-ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    org = Organization(
        workspace_id=ws.id,
        name="test-org",
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider=provider,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return ws, org


def _setup_user_with_identity_and_memberships(
    db: Session,
    org: Organization,
    user_external_id: str = "gh-999",
    role: str = "member",
) -> tuple[User, ProviderIdentity]:
    user = User(email=f"user-{uuid.uuid4().hex[:6]}@example.com")
    db.add(user)
    db.flush()

    identity = ProviderIdentity(
        provider="github",
        external_id=user_external_id,
        username=f"ghuser-{uuid.uuid4().hex[:4]}",
        user_id=user.id,
    )
    db.add(identity)
    db.flush()

    db.add(OrgMembership(org_id=org.id, provider_identity_id=identity.id, role=role))
    if org.workspace_id:
        db.add(WorkspaceMembership(workspace_id=org.workspace_id, user_id=user.id))
    db.commit()
    db.refresh(identity)
    return user, identity


# ---------------------------------------------------------------------------
# handle_member_removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_removed_revokes_workspace_membership(db: Session) -> None:
    """Removing the user's only org in a workspace should also remove WorkspaceMembership."""
    ws, org = _setup_org_with_workspace(db)
    user, identity = _setup_user_with_identity_and_memberships(db, org)

    event = _make_event(
        "member_removed", org.external_org_id, identity.external_id, identity.username
    )
    response = handle_member_removed(event, db)

    assert response.processed is True

    remaining_ws = (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == ws.id,
            WorkspaceMembership.user_id == user.id,
        )
        .first()
    )
    assert remaining_ws is None


@pytest.mark.asyncio
async def test_member_removed_retains_workspace_membership_when_other_org_exists(
    db: Session,
) -> None:
    """User removed from one org but still in another org in same workspace keeps access."""
    ws, org1 = _setup_org_with_workspace(db)
    # Second org in the same workspace
    org2 = Organization(
        workspace_id=ws.id,
        name="org-two",
        external_org_id=f"org2-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add(org2)
    db.commit()

    user, identity = _setup_user_with_identity_and_memberships(db, org1)
    # Also a member of org2
    db.add(OrgMembership(org_id=org2.id, provider_identity_id=identity.id, role="member"))
    db.commit()

    event = _make_event(
        "member_removed", org1.external_org_id, identity.external_id, identity.username
    )
    handle_member_removed(event, db)

    ws_membership = (
        db.query(WorkspaceMembership)
        .filter(
            WorkspaceMembership.workspace_id == ws.id,
            WorkspaceMembership.user_id == user.id,
        )
        .first()
    )
    assert ws_membership is not None


@pytest.mark.asyncio
async def test_member_removed_no_workspace_no_error(db: Session) -> None:
    """Org without a workspace_id — removal should succeed without touching workspace memberships."""
    org = Organization(
        workspace_id=None,
        name="unattached-org",
        external_org_id=f"uorg-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db.add(org)
    db.commit()

    user = User(email="nowws@example.com")
    db.add(user)
    db.flush()
    identity = ProviderIdentity(
        provider="github",
        external_id="gh-no-ws",
        username="nowws-user",
        user_id=user.id,
    )
    db.add(identity)
    db.flush()
    db.add(OrgMembership(org_id=org.id, provider_identity_id=identity.id, role="member"))
    db.commit()

    event = _make_event("member_removed", org.external_org_id, "gh-no-ws", "nowws-user")
    response = handle_member_removed(event, db)

    assert response.processed is True


@pytest.mark.asyncio
async def test_member_removed_unknown_identity_is_noop(db: Session) -> None:
    """Webhook for a user we've never seen should be a clean no-op."""
    _ws, org = _setup_org_with_workspace(db)

    event = _make_event("member_removed", org.external_org_id, "gh-unknown-9999", "ghost")
    response = handle_member_removed(event, db)

    assert response.processed is True


# ---------------------------------------------------------------------------
# handle_member_added (smoke test to ensure it still works)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_added_creates_memberships(db: Session) -> None:
    """handle_member_added should create OrgMembership and WorkspaceMembership."""
    _ws, org = _setup_org_with_workspace(db)

    event = _make_event("member_added", org.external_org_id, "gh-new-42", "newuser", role="member")
    response = handle_member_added(event, db)

    assert response.processed is True

    identity = (
        db.query(ProviderIdentity).filter(ProviderIdentity.external_id == "gh-new-42").first()
    )
    assert identity is not None

    org_m = (
        db.query(OrgMembership).filter(OrgMembership.provider_identity_id == identity.id).first()
    )
    assert org_m is not None
