"""``GET /organizations/{org_id}/members`` — the notify picker's candidates."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routers.organizations.route import list_organization_members


def _identity(username, *, provider="gitlab", user_id=None, avatar=None):
    identity = MagicMock()
    identity.id = uuid4()
    identity.username = username
    identity.provider = provider
    identity.avatar_url = avatar
    identity.user_id = user_id
    return identity


def _membership(role="member"):
    membership = MagicMock()
    membership.role = role
    return membership


def _org():
    org = MagicMock()
    org.id = uuid4()
    org.provider = "gitlab"
    return org


def _call(members, *, org=None):
    with (
        patch("api.routers.organizations.route.verify_org_access_from_body"),
        patch("api.routers.organizations.route.db_get_org_by_id", return_value=org or _org()),
        patch("api.routers.organizations.route.db_get_org_members", return_value=members),
    ):
        return list_organization_members(org_id=uuid4(), current_user=MagicMock(), db=MagicMock())


def test_returns_members_with_their_identity_ids():
    alice = _identity("alice", avatar="https://example.com/a.png")
    response = _call([(_membership("owner"), alice)])

    assert response.total == 1
    member = response.members[0]
    assert member.username == "alice"
    assert member.provider_identity_id == alice.id
    assert member.role == "owner"
    assert member.avatar_url == "https://example.com/a.png"


def test_members_without_a_jeanclode_account_are_still_offered():
    """A provider member synced by ``sync_org_members`` has no User row yet;
    excluding them would hide exactly the people worth notifying."""
    response = _call(
        [
            (_membership(), _identity("never-logged-in", user_id=None)),
            (_membership(), _identity("has-account", user_id=uuid4())),
        ]
    )

    by_name = {m.username: m.has_account for m in response.members}
    assert by_name == {"never-logged-in": False, "has-account": True}


def test_identities_without_a_username_are_skipped():
    """A handle is what gets @-mentioned, so a nameless identity is not a
    selectable member."""
    response = _call([(_membership(), _identity(None)), (_membership(), _identity("bob"))])

    assert [m.username for m in response.members] == ["bob"]
    assert response.total == 1


def test_empty_org_returns_an_empty_list():
    response = _call([])
    assert response.members == []
    assert response.total == 0


def test_missing_org_is_404():
    with (
        patch("api.routers.organizations.route.verify_org_access_from_body"),
        patch("api.routers.organizations.route.db_get_org_by_id", return_value=None),
        pytest.raises(HTTPException) as exc_info,
    ):
        list_organization_members(org_id=uuid4(), current_user=MagicMock(), db=MagicMock())

    assert exc_info.value.status_code == 404
