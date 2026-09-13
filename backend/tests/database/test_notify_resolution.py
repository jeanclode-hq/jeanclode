"""Notify-list resolution: org member chain → @-mention handles.

The ids in settings are provider identities, deliberately not handles, so
resolution is where a rename, a departure, or a wrong-provider id has to be
caught — the output of this is live text posted into a customer's repo.
"""

import pytest

from api.database.organization import (
    db_get_org_chain_ids,
    db_get_org_members,
    db_resolve_notify_handles,
)
from api.models import Base
from api.models.identities import ProviderIdentity
from api.models.organizations import Organization, OrgMembership
from api.models.workspaces import Workspace


@pytest.fixture
def db(app):
    Base.metadata.create_all(bind=app.database.engine)
    session = app.database.get_session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=app.database.engine)


def _identity(db, username, provider="gitlab", user_id=None, avatar=None):
    identity = ProviderIdentity(
        provider=provider,
        external_id=f"ext-{provider}-{username}",
        username=username,
        avatar_url=avatar,
        user_id=user_id,
    )
    db.add(identity)
    db.flush()
    return identity


def _org(db, workspace, name, *, parent=None, provider="gitlab"):
    org = Organization(
        workspace_id=workspace.id,
        name=name,
        external_org_id=f"id-{name}",
        provider=provider,
        parent_org_id=parent.id if parent else None,
        root_org_id=(parent.root_org_id or parent.id) if parent else None,
    )
    db.add(org)
    db.flush()
    return org


def _member(db, org, identity, role="member"):
    db.add(OrgMembership(org_id=org.id, provider_identity_id=identity.id, role=role))
    db.flush()


@pytest.fixture
def group_chain(db):
    """A GitLab group with a subgroup — members live on the group only."""
    ws = Workspace(name="ws", slug="ws")
    db.add(ws)
    db.flush()
    group = _org(db, ws, "group")
    subgroup = _org(db, ws, "subgroup", parent=group)
    alice = _identity(db, "alice")
    bob = _identity(db, "bob")
    _member(db, group, alice, role="owner")
    _member(db, group, bob)
    db.commit()
    return {"group": group, "subgroup": subgroup, "alice": alice, "bob": bob, "ws": ws}


def test_chain_walks_up_to_the_root(db, group_chain):
    chain = db_get_org_chain_ids(db, group_chain["subgroup"])
    assert chain == [group_chain["subgroup"].id, group_chain["group"].id]


def test_subgroup_sees_the_group_members(db, group_chain):
    """A subgroup org has no memberships of its own, so resolving against it
    alone would silently notify nobody."""
    members = db_get_org_members(db, group_chain["subgroup"])
    assert [i.username for _m, i in members] == ["alice", "bob"]


def test_resolves_ids_to_handles_in_the_configured_order(db, group_chain):
    handles = db_resolve_notify_handles(
        db, group_chain["subgroup"], [group_chain["bob"].id, group_chain["alice"].id]
    )
    assert handles == ["bob", "alice"]


def test_empty_list_resolves_to_nothing(db, group_chain):
    assert db_resolve_notify_handles(db, group_chain["group"], []) == []


def test_identity_outside_the_org_is_dropped(db, group_chain):
    """Someone removed from the group must stop being notified, even though
    their id is still sitting in settings."""
    outsider = _identity(db, "eve")
    db.commit()

    handles = db_resolve_notify_handles(
        db, group_chain["group"], [group_chain["alice"].id, outsider.id]
    )
    assert handles == ["alice"]


def test_wrong_provider_identity_is_dropped(db, group_chain):
    """A GitHub identity must never resolve to a handle @-mentioned into a
    GitLab MR — it would ping an unrelated stranger on that instance."""
    ws = group_chain["ws"]
    gh_org = _org(db, ws, "gh-org", provider="github")
    gh_identity = _identity(db, "ghost", provider="github")
    _member(db, gh_org, gh_identity)
    # Same identity also carries membership in the gitlab group.
    _member(db, group_chain["group"], gh_identity)
    db.commit()

    handles = db_resolve_notify_handles(
        db, group_chain["group"], [group_chain["alice"].id, gh_identity.id]
    )
    assert handles == ["alice"]


def test_identity_without_a_username_is_dropped(db, group_chain):
    nameless = _identity(db, None)
    _member(db, group_chain["group"], nameless)
    db.commit()

    handles = db_resolve_notify_handles(db, group_chain["group"], [nameless.id])
    assert handles == []


def test_duplicate_ids_collapse(db, group_chain):
    handles = db_resolve_notify_handles(
        db, group_chain["group"], [group_chain["alice"].id, group_chain["alice"].id]
    )
    assert handles == ["alice"]


def test_nearest_org_wins_the_role_on_a_duplicate_identity(db, group_chain):
    """Same person a member of both group and subgroup — the more specific
    role is the one shown."""
    _member(db, group_chain["subgroup"], group_chain["alice"], role="admin")
    db.commit()

    members = {i.username: m.role for m, i in db_get_org_members(db, group_chain["subgroup"])}
    assert members["alice"] == "admin"


def test_members_include_identities_with_no_jeanclode_account(db, group_chain):
    """The maintainer who has never logged in is exactly who a tenant wants
    to notify, so they have to be selectable."""
    members = db_get_org_members(db, group_chain["group"])
    assert all(i.user_id is None for _m, i in members)
    assert len(members) == 2
