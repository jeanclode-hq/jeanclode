"""Database operations for MCP servers and their shared credential store."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from api.models.connectors import Credential, McpServer

# ---------------------------------------------------------------------------
# McpServer
# ---------------------------------------------------------------------------


def db_get_mcp_servers_by_org(db: Session, org_id: UUID) -> list[McpServer]:
    return db.query(McpServer).filter(McpServer.org_id == org_id).order_by(McpServer.name).all()


def db_get_mcp_server_by_id(db: Session, mcp_server_id: UUID) -> McpServer | None:
    return db.query(McpServer).filter(McpServer.id == mcp_server_id).first()


def db_create_mcp_server(db: Session, *, org_id: UUID, name: str, host: str) -> McpServer:
    server = McpServer(org_id=org_id, name=name, host=host)
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def db_update_mcp_server(db: Session, server: McpServer, **kwargs: object) -> McpServer:
    for key, value in kwargs.items():
        setattr(server, key, value)
    db.commit()
    db.refresh(server)
    return server


def db_delete_mcp_server(db: Session, server: McpServer) -> None:
    db.query(Credential).filter(
        Credential.subject_type == "mcp_server", Credential.subject_id == server.id
    ).delete()
    db.delete(server)
    db.commit()


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------


def db_get_credential_by_subject(
    db: Session, *, subject_type: str, subject_id: UUID
) -> Credential | None:
    return (
        db.query(Credential)
        .filter(Credential.subject_type == subject_type, Credential.subject_id == subject_id)
        .first()
    )


def db_get_credentials_by_org(db: Session, org_id: UUID) -> list[Credential]:
    return db.query(Credential).filter(Credential.org_id == org_id).all()


def db_upsert_credential(
    db: Session,
    *,
    org_id: UUID,
    subject_type: str,
    subject_id: UUID,
    auth_type: str,
    settings: dict,
    secret_encrypted: str,
) -> Credential:
    """Create or fully replace the credential for a subject.

    Full-replace, not a merge — the write route (unlike the org-settings
    ``PATCH``) always carries a complete secret payload, so there's no
    "leave the old secret if the field is missing" ambiguity to worry
    about; every call here is a deliberate set-or-overwrite of the whole
    credential for that subject.
    """
    existing = db_get_credential_by_subject(db, subject_type=subject_type, subject_id=subject_id)
    if existing:
        existing.auth_type = auth_type
        existing.settings = settings
        existing.secret_encrypted = secret_encrypted
        db.commit()
        db.refresh(existing)
        return existing

    credential = Credential(
        org_id=org_id,
        subject_type=subject_type,
        subject_id=subject_id,
        auth_type=auth_type,
        settings=settings,
        secret_encrypted=secret_encrypted,
    )
    db.add(credential)
    db.commit()
    db.refresh(credential)
    return credential


def db_delete_credential_by_subject(db: Session, *, subject_type: str, subject_id: UUID) -> None:
    db.query(Credential).filter(
        Credential.subject_type == subject_type, Credential.subject_id == subject_id
    ).delete()
    db.commit()
