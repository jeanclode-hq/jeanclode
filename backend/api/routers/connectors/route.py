"""MCP server registration + generic connector credential store.

* ``GET/POST/PATCH/DELETE /mcp-servers`` — CRUD for an org's registered
  remote MCP servers. Client-only: ``host`` is always a URL to connect to,
  never a process to spawn.
* ``GET/PUT/DELETE /credentials`` — auth for a subject (an MCP server or an
  installed skill). ``GET`` never returns the secret; ``PUT`` is the only
  way to set/replace it (token in, nothing back).

See https://github.com/jeanclode-hq/jeanclode/issues/14 for the full design.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import (
    db_create_mcp_server,
    db_delete_credential_by_subject,
    db_delete_mcp_server,
    db_get_credential_by_subject,
    db_get_installation_by_id,
    db_get_mcp_server_by_id,
    db_get_mcp_servers_by_org,
    db_get_org_by_id,
    db_update_mcp_server,
    db_upsert_credential,
    get_session,
)
from api.models import Credential, McpServer, User
from api.models.connectors import AuthType, SubjectType
from api.routers.auth.dependencies import get_current_user, verify_org_access_from_body
from api.routers.connectors.utils import credential_to_status

from .schemas import (
    CreateMcpServerRequest,
    CredentialStatus,
    McpServerResponse,
    UpdateMcpServerRequest,
    WriteCredentialRequest,
    secret_model_for,
    settings_model_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Connectors"])

# The CLI always registers this name for the in-process memory MCP server
# (cli/src/agents/memory_tool.py:MEMORY_SERVER_NAME). An org-registered MCP
# server with the same name would silently overwrite it in the same
# ClaudeAgentOptions.mcp_servers dict — reject it here rather than let that
# collision happen at dispatch time.
_RESERVED_MCP_SERVER_NAMES = frozenset({"memory"})


def _reject_reserved_mcp_server_name(name: str | None) -> None:
    if name is not None and name.strip().lower() in _RESERVED_MCP_SERVER_NAMES:
        raise HTTPException(status_code=422, detail=f"'{name}' is a reserved MCP server name")


def _mcp_server_to_response(server: McpServer, has_credential: bool) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        org_id=server.org_id,
        name=server.name,
        host=server.host,
        has_credential=has_credential,
    )


def _verify_subject(
    db: Session, *, subject_type: SubjectType, subject_id: uuid.UUID, org_id: uuid.UUID
) -> None:
    """Confirm the subject exists and belongs to the claimed org.

    Without this, a caller could attach a credential to a subject_id
    belonging to a different org's row — subject_id carries no DB-level FK
    (polymorphic across two tables), so this is the only check.
    """
    if subject_type == SubjectType.MCP_SERVER:
        server = db_get_mcp_server_by_id(db, subject_id)
        if not server or server.org_id != org_id:
            raise HTTPException(status_code=404, detail="MCP server not found")
        return
    install = db_get_installation_by_id(db, subject_id)
    if not install or install.org_id != org_id:
        raise HTTPException(status_code=404, detail="Plugin installation not found")


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@router.get(
    "/mcp-servers",
    operation_id="list_mcp_servers",
    response_model=list[McpServerResponse],
)
def list_mcp_servers(
    org_id: uuid.UUID = Query(..., description="Organization ID"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[McpServerResponse]:
    verify_org_access_from_body(db, current_user, org_id)

    servers = db_get_mcp_servers_by_org(db, org_id)
    server_ids = {s.id for s in servers}
    credentialed = (
        {
            c.subject_id
            for c in db.query(Credential).filter(
                Credential.subject_type == SubjectType.MCP_SERVER.value,
                Credential.subject_id.in_(server_ids),
            )
        }
        if server_ids
        else set()
    )

    return [_mcp_server_to_response(s, s.id in credentialed) for s in servers]


@router.post(
    "/mcp-servers",
    operation_id="create_mcp_server",
    response_model=McpServerResponse,
    status_code=201,
)
def create_mcp_server(
    request: CreateMcpServerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> McpServerResponse:
    """Register a remote MCP server for an org.

    Only meaningful for git orgs (github/gitlab) — Jeanclode dispatches MCP
    connections alongside the git-provider-scoped agent run, same as skills.
    """
    verify_org_access_from_body(db, current_user, request.org_id)

    org = db_get_org_by_id(db, request.org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.provider not in ("github", "gitlab"):
        raise HTTPException(
            status_code=400, detail="MCP servers can only be added to a git organization"
        )
    _reject_reserved_mcp_server_name(request.name)

    server = db_create_mcp_server(db, org_id=request.org_id, name=request.name, host=request.host)
    return _mcp_server_to_response(server, has_credential=False)


@router.patch(
    "/mcp-servers/{mcp_server_id}",
    operation_id="update_mcp_server",
    response_model=McpServerResponse,
)
def update_mcp_server(
    mcp_server_id: uuid.UUID,
    request: UpdateMcpServerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> McpServerResponse:
    server = db_get_mcp_server_by_id(db, mcp_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    verify_org_access_from_body(db, current_user, server.org_id)
    _reject_reserved_mcp_server_name(request.name)

    updates = request.model_dump(exclude_none=True)
    if updates:
        server = db_update_mcp_server(db, server, **updates)

    has_credential = (
        db_get_credential_by_subject(
            db, subject_type=SubjectType.MCP_SERVER.value, subject_id=server.id
        )
        is not None
    )
    return _mcp_server_to_response(server, has_credential)


@router.delete(
    "/mcp-servers/{mcp_server_id}",
    operation_id="delete_mcp_server",
    status_code=204,
)
def delete_mcp_server(
    mcp_server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    server = db_get_mcp_server_by_id(db, mcp_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    verify_org_access_from_body(db, current_user, server.org_id)
    db_delete_mcp_server(db, server)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.get(
    "/credentials",
    operation_id="get_credential",
    response_model=CredentialStatus | None,
)
def get_credential(
    subject_type: SubjectType = Query(...),
    subject_id: uuid.UUID = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> CredentialStatus | None:
    """Status only — the secret itself is never returned."""
    cred = db_get_credential_by_subject(db, subject_type=subject_type.value, subject_id=subject_id)
    if not cred:
        return None
    verify_org_access_from_body(db, current_user, cred.org_id)
    return credential_to_status(cred)


@router.put(
    "/credentials",
    operation_id="write_credential",
    response_model=CredentialStatus,
)
def write_credential(
    request: WriteCredentialRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> CredentialStatus:
    """Set or replace the credential for a subject. Token in, nothing back.

    Full-replace by design — the frontend never has the previous secret to
    merge against (``GET`` omits it), so a partial update would silently
    have to guess at missing fields.
    """
    verify_org_access_from_body(db, current_user, request.org_id)
    _verify_subject(
        db, subject_type=request.subject_type, subject_id=request.subject_id, org_id=request.org_id
    )

    secret_cls = secret_model_for(request.auth_type)
    settings_cls = settings_model_for(request.auth_type)
    try:
        validated_secret = secret_cls.model_validate(request.secret)
        validated_settings = settings_cls.model_validate(request.settings)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

    # A skill has no host column of its own (unlike McpServer.host) — the
    # proxy needs settings.host to know what to inject this credential into.
    if request.subject_type == SubjectType.PLUGIN_INSTALLATION and not getattr(
        validated_settings, "host", None
    ):
        raise HTTPException(
            status_code=422, detail="settings.host is required for a skill credential"
        )

    # OAuth2Secret can't validate this itself — grant_type lives on the
    # sibling OAuth2Settings model, validated separately above.
    if request.auth_type == AuthType.OAUTH2:
        grant_type = getattr(validated_settings, "grant_type", "client_credentials")
        if grant_type == "password" and not (
            getattr(validated_secret, "username", None)
            and getattr(validated_secret, "password", None)
        ):
            raise HTTPException(
                status_code=422,
                detail="secret.username and secret.password are required for the password grant",
            )
        if grant_type == "refresh_token" and not getattr(validated_secret, "refresh_token", None):
            raise HTTPException(
                status_code=422,
                detail="secret.refresh_token is required for the refresh_token grant",
            )

    app = get_current_app()
    if not app.database:
        raise HTTPException(status_code=503, detail="Database not available")

    secret_encrypted = app.database.encrypt(json.dumps(validated_secret.model_dump()))

    cred = db_upsert_credential(
        db,
        org_id=request.org_id,
        subject_type=request.subject_type.value,
        subject_id=request.subject_id,
        auth_type=request.auth_type.value,
        settings=validated_settings.model_dump(),
        secret_encrypted=secret_encrypted,
    )
    return credential_to_status(cred)


@router.delete(
    "/credentials",
    operation_id="delete_credential",
    status_code=204,
)
def delete_credential(
    subject_type: SubjectType = Query(...),
    subject_id: uuid.UUID = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    cred = db_get_credential_by_subject(db, subject_type=subject_type.value, subject_id=subject_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    verify_org_access_from_body(db, current_user, cred.org_id)
    db_delete_credential_by_subject(db, subject_type=subject_type.value, subject_id=subject_id)
