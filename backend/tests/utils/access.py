"""Test helper for granting dashboard access to an org."""

from uuid import UUID

from sqlalchemy.orm import Session

from api.database.workspace import db_is_workspace_member
from api.models.organizations import Organization
from api.models.workspaces import WorkspaceMembership


def grant_org_access(db: Session, org: Organization, user_id: UUID | str) -> None:
    """Give a user access to ``org``.

    Access is workspace-scoped — a member of the org's workspace sees every
    connection in it — so this writes a WorkspaceMembership, not an
    OrgMembership. Idempotent, since several orgs often share one workspace.
    """
    uid = UUID(str(user_id))
    if db_is_workspace_member(db, org.workspace_id, uid):
        return
    db.add(WorkspaceMembership(workspace_id=org.workspace_id, user_id=uid))
    db.commit()
