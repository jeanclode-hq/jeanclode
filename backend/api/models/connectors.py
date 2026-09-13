"""Generic per-org credential store for skills and MCP servers.

Two tables:

- ``mcp_servers`` — remote MCP servers an org has registered. Jeanclode is a
  pure MCP *client*: every entry is a URL to connect out to, never a process
  to spawn. Same shape as ``PluginInstallation`` (id/org_id/name), so its
  CRUD mirrors the existing plugin-installation routes.
- ``credentials`` — the generic auth store shared by MCP servers *and*
  installed skills (``PluginInstallation``). ``subject_type``/``subject_id``
  point at whichever row the credential authenticates; ``UniqueConstraint``
  enforces zero-or-one credential per subject. Non-secret config
  (header name, token endpoint, …) lives in ``settings`` (JSONB); the
  actual secret value(s) live in ``secret_encrypted``, an AES-256-GCM
  blob (via ``DatabasePlugin.encrypt``/``decrypt``) holding a JSON object
  whose shape depends on ``auth_type`` — see ``api/routers/connectors/schemas.py``.

See https://github.com/jeanclode-hq/jeanclode/issues/14 for the full design.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class SubjectType(StrEnum):
    """What a credential authenticates."""

    PLUGIN_INSTALLATION = "plugin_installation"
    MCP_SERVER = "mcp_server"


class AuthType(StrEnum):
    """Shape of the stored secret and how the proxy injects it."""

    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"
    JWT = "jwt"
    OAUTH2 = "oauth2"


class McpServer(Base):
    """A remote MCP server registered for an organization.

    Client-only: ``host`` is always a URL Jeanclode connects out to over
    HTTP — there is no stdio/spawn path.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_mcp_server_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Credential(Base):
    """Auth for one subject (an installed plugin or an MCP server).

    ``subject_id`` is a polymorphic reference (no FK — the two subject
    tables are unrelated); callers must delete the matching ``Credential``
    row themselves when deleting a ``PluginInstallation``/``McpServer``.
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("subject_type", "subject_id", name="uq_credential_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[uuid.UUID] = mapped_column()

    auth_type: Mapped[str] = mapped_column(String(50))
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)
    secret_encrypted: Mapped[str] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
