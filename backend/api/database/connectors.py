"""Database operations for MCP servers and their shared credential store."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
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


def db_get_credentials_by_subject(
    db: Session, *, subject_type: str, subject_id: UUID
) -> list[Credential]:
    return (
        db.query(Credential)
        .filter(Credential.subject_type == subject_type, Credential.subject_id == subject_id)
        .order_by(Credential.created_at, Credential.id)
        .all()
    )


def db_get_credential_by_id(db: Session, credential_id: UUID) -> Credential | None:
    return db.query(Credential).filter(Credential.id == credential_id).first()


def db_get_credentials_by_org(db: Session, org_id: UUID) -> list[Credential]:
    return (
        db.query(Credential)
        .filter(Credential.org_id == org_id)
        .order_by(Credential.created_at, Credential.id)
        .all()
    )


def db_count_credentials_by_subjects(
    db: Session, *, subject_type: str, subject_ids: set[UUID]
) -> dict[UUID, int]:
    if not subject_ids:
        return {}
    rows = (
        db.query(Credential.subject_id, func.count(Credential.id))
        .filter(Credential.subject_type == subject_type, Credential.subject_id.in_(subject_ids))
        .group_by(Credential.subject_id)
        .all()
    )
    return dict(rows)


def db_create_credential(
    db: Session,
    *,
    org_id: UUID,
    subject_type: str,
    subject_id: UUID,
    auth_type: str,
    settings: dict,
    secret_encrypted: str,
) -> Credential:
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


def db_replace_credential(
    db: Session, credential: Credential, *, auth_type: str, settings: dict, secret_encrypted: str
) -> Credential:
    """Full replace — the write route always carries a complete secret, since
    the frontend never has the old one to merge against."""
    credential.auth_type = auth_type
    credential.settings = settings
    credential.secret_encrypted = secret_encrypted
    db.commit()
    db.refresh(credential)
    return credential


def db_delete_credential(db: Session, credential: Credential) -> None:
    db.delete(credential)
    db.commit()


def db_delete_credentials_by_subject(db: Session, *, subject_type: str, subject_id: UUID) -> None:
    db.query(Credential).filter(
        Credential.subject_type == subject_type, Credential.subject_id == subject_id
    ).delete()
    db.commit()
