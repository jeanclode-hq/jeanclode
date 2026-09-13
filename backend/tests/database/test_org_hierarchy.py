"""Tests for GitLab group/subgroup hierarchy behaviour in the org DB layer.

A GitLab group is connected once. The repo sync then creates an org per
subgroup namespace to hold that subgroup's projects — orgs with no token, no
membership and no presence in the UI. Everything keyed off an org has to
reach through them, or the projects they hold behave as if they belonged to
nobody.
"""

import uuid

from sqlalchemy.orm import Session

from api.database.organization import (
    db_get_connection_org_id,
    db_get_org_subtree_ids,
    db_resolve_org_settings,
)
from api.models.organizations import Organization
from api.models.workspaces import Workspace


def _workspace(db: Session) -> Workspace:
    ws = Workspace(name="hierarchy-ws", slug=f"hw-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    return ws


def _org(db: Session, ws: Workspace, name: str, **kwargs) -> Organization:
    org = Organization(
        workspace_id=ws.id,
        name=name,
        external_org_id=f"{name}-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        **kwargs,
    )
    db.add(org)
    db.flush()
    return org


def test_subtree_ids_span_every_level(db_session: Session):
    ws = _workspace(db_session)
    group = _org(db_session, ws, "group", auth_token_encrypted="token")
    sub = _org(db_session, ws, "sub", parent_org_id=group.id, root_org_id=group.id)
    nested = _org(db_session, ws, "nested", parent_org_id=sub.id, root_org_id=group.id)
    sibling = _org(db_session, ws, "sibling", auth_token_encrypted="other")
    db_session.commit()

    ids = db_get_org_subtree_ids(db_session, [group.id], ws.id)
    assert set(ids) == {group.id, sub.id, nested.id}
    assert sibling.id not in ids


def test_settings_are_inherited_from_the_connected_group(db_session: Session):
    """A subgroup with no settings of its own runs on the group's triggers."""
    ws = _workspace(db_session)
    group = _org(
        db_session,
        ws,
        "group",
        auth_token_encrypted="token",
        settings={"triggers": {"review": "on_pr_creation", "summary": "manual"}},
    )
    sub = _org(db_session, ws, "sub", parent_org_id=group.id, root_org_id=group.id)
    db_session.commit()

    assert db_resolve_org_settings(db_session, sub) == {
        "triggers": {"review": "on_pr_creation", "summary": "manual"}
    }


def test_a_subgroups_own_setting_still_wins(db_session: Session):
    """Inheritance layers root-first, so a nearer org overrides one key only."""
    ws = _workspace(db_session)
    group = _org(
        db_session,
        ws,
        "group",
        auth_token_encrypted="token",
        settings={"triggers": {"review": "on_pr_creation", "summary": "manual"}},
    )
    sub = _org(
        db_session,
        ws,
        "sub",
        parent_org_id=group.id,
        root_org_id=group.id,
        settings={"triggers": {"review": "manual"}},
    )
    db_session.commit()

    assert db_resolve_org_settings(db_session, sub) == {
        "triggers": {"review": "manual", "summary": "manual"}
    }


def test_connection_org_resolves_up_to_the_connected_group(db_session: Session):
    """Connectors and plugins hang off the connected group, not the subgroup.

    A run on a project inside a subgroup is keyed to that subgroup's org,
    which holds no MCP servers, credentials or plugin installs of its own.
    """
    ws = _workspace(db_session)
    group = _org(db_session, ws, "group", auth_token_encrypted="token")
    sub = _org(db_session, ws, "sub", parent_org_id=group.id, root_org_id=group.id)
    nested = _org(db_session, ws, "nested", parent_org_id=sub.id, root_org_id=group.id)
    db_session.commit()

    assert db_get_connection_org_id(db_session, nested.id) == group.id
    assert db_get_connection_org_id(db_session, sub.id) == group.id
    assert db_get_connection_org_id(db_session, group.id) == group.id


def test_connection_org_of_a_project_token_namespace_is_itself(db_session: Session):
    """A namespace connected by a project access token has no org-level token
    and no connected ancestor — it is the connection.
    """
    ws = _workspace(db_session)
    placeholder = _org(db_session, ws, "placeholder")
    namespace = _org(
        db_session, ws, "namespace", parent_org_id=placeholder.id, root_org_id=placeholder.id
    )
    db_session.commit()

    assert db_get_connection_org_id(db_session, namespace.id) == namespace.id
