"""Auth for container-initiated backend calls (the ``/internal`` namespace).

Verifies a signed, per-execution, workspace-scoped token minted by
``api.plugins.container.dispatch_inputs.add_memory_to_inputs``. Every route
in ``api.routers.internal.memory`` must scope its DB queries off the
returned principal's ``workspace_id`` only — never a client-supplied field.
"""

from uuid import UUID

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.database import get_session
from api.services.instance_settings import get_or_create_memory_signing_secret
from api.services.memory_token import MemoryTokenError, verify_memory_token


class MemoryPrincipal(BaseModel):
    """Resolved scope for a container-initiated memory request."""

    workspace_id: UUID


def get_memory_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> MemoryPrincipal:
    """Verify a signed memory API token from the Authorization header.

    Raises:
        HTTPException: 401 for any missing/malformed/expired/tampered token.
    """
    secret = get_or_create_memory_signing_secret(db)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = verify_memory_token(token, secret=secret)
    except MemoryTokenError:
        raise HTTPException(status_code=401, detail="Invalid token") from None

    return MemoryPrincipal(workspace_id=payload.workspace_id)
