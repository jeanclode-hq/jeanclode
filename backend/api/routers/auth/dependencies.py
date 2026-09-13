"""FastAPI dependencies for authentication and authorization."""

import logging
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload

from api.context import get_current_app
from api.database.organization import db_get_org_by_id
from api.database.workspace import db_is_workspace_member
from api.models import User
from api.plugins.web.session import SessionManager

from .enums import OAuthProvider

logger = logging.getLogger(__name__)


def _get_sessions() -> SessionManager:
    """Get the session manager from the web plugin."""
    app = get_current_app()
    if not app.web:
        raise HTTPException(status_code=503, detail="Web plugin not available")
    return app.web.sessions


# =============================================================================
# Authentication
# =============================================================================


async def get_current_user(
    jeanclode_session: str | None = Cookie(None),
) -> User:
    """Dependency to get the current authenticated user from Redis session.

    Reads session ID from cookie, looks up session in Redis,
    loads user from DB with eager-loaded identities.

    Raises:
        HTTPException: If not authenticated or user not found
    """
    if not jeanclode_session:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sessions = _get_sessions()
    session_data = await sessions.get(jeanclode_session)
    if not session_data:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    await sessions.refresh(jeanclode_session, session_data)

    user_id = UUID(session_data.user_id)
    app = get_current_app()
    if not app.database:
        raise HTTPException(status_code=503, detail="Database not available")

    def _load(db: Session) -> User | None:
        return (
            db.query(User).options(selectinload(User.identities)).filter(User.id == user_id).first()
        )

    user = await app.database.run_in_session(_load)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


async def require_provider_enabled(provider: OAuthProvider) -> None:
    """Raises 503 if the given OAuth provider is not configured."""
    app = get_current_app()
    if not app.oauth or not app.oauth.is_provider_enabled(provider.value):
        raise HTTPException(
            status_code=503,
            detail=f"{provider.value.capitalize()} login is not available",
        )


# =============================================================================
# Authorization — workspace access
# =============================================================================


async def verify_workspace_access(
    workspace_id: str = Query(..., description="Workspace ID"),
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that verifies the user is a member of the given workspace.

    Returns:
        The authenticated user

    Raises:
        HTTPException: 403 if user is not a workspace member
    """
    app = get_current_app()
    if not app.database:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        ws_id = UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace ID") from e

    is_member = await app.database.run_in_session(
        lambda db: db_is_workspace_member(db, ws_id, current_user.id)
    )
    if not is_member:
        raise HTTPException(status_code=403, detail="You don't have access to this workspace")

    return current_user


# =============================================================================
# Authorization — organization access
# =============================================================================


# Access to an org is workspace-scoped on purpose: any member of the workspace
# an org belongs to can see and manage it, whether or not they hold a
# membership on the provider side. Mirroring provider memberships (Sentry's
# especially) left legitimate teammates locked out of connections their own
# workspace owns.


def _org_workspace_id(db: Session, org_id: UUID) -> UUID:
    """Workspace owning ``org_id``, or 403 if the org is unknown/unclaimed."""
    org = db_get_org_by_id(db, org_id)
    if not org or org.workspace_id is None:
        raise HTTPException(status_code=403, detail="You don't have access to this organization")
    return org.workspace_id


async def verify_org_access(
    org_id: UUID = Query(..., description="Organization ID"),
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that verifies the user is a member of the org's workspace.

    Returns:
        The authenticated user

    Raises:
        HTTPException: 403 if the user is not a member of that workspace
    """
    app = get_current_app()
    if not app.database:
        raise HTTPException(status_code=503, detail="Database not available")

    def _check(db: Session) -> bool:
        return db_is_workspace_member(db, _org_workspace_id(db, org_id), current_user.id)

    if not await app.database.run_in_session(_check):
        raise HTTPException(status_code=403, detail="You don't have access to this organization")

    return current_user


# Both helpers below take the handler's own session rather than opening one.
# They run mid-handler, while that session is holding a pooled connection for
# an uncommitted transaction, so a session of their own would make every such
# request occupy two slots of the pool at once — halving how many dashboard
# users the pool can serve, and exhausting it under an ordinary burst.


def verify_org_access_from_body(db: Session, user: User, org_id: UUID) -> None:
    """Check org access when org_id comes from a path param or request body.

    Call this inside the route handler after extracting the param, passing the
    handler's own session.

    Raises:
        HTTPException: 403 if the user is not a member of the org's workspace
    """
    if not db_is_workspace_member(db, _org_workspace_id(db, org_id), user.id):
        raise HTTPException(status_code=403, detail="You don't have access to this organization")


def verify_workspace_access_from_path(db: Session, user: User, workspace_id: UUID) -> None:
    """Check workspace access when workspace_id comes from a path param.

    Call this inside the route handler after extracting the path param,
    passing the handler's own session.

    Raises:
        HTTPException: 403 if user has no membership in the workspace
    """
    if not db_is_workspace_member(db, workspace_id, user.id):
        raise HTTPException(status_code=403, detail="You don't have access to this workspace")
