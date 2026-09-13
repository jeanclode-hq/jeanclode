"""Plugin marketplace and installation models.

Two tables:
- ``plugin_marketplaces`` — which marketplace repos an org has connected.
- ``plugin_installations`` — which plugins an org has installed from those
  marketplaces, plus per-workflow/project settings.

Plugin content is never snapshotted — at dispatch the CLI shallow-clones the
pinned version and loads it via the standard Claude Code plugin mechanism.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base


class MarketplaceStatus(StrEnum):
    """Sync state of a connected marketplace."""

    OK = "ok"
    ERROR = "error"


class PluginMarketplace(Base):
    """A git-hosted marketplace connected to an organization.

    A marketplace is a git repo containing ``.claude-plugin/marketplace.json``.
    We store only the pointer; the manifest is re-fetched on demand.
    """

    __tablename__ = "plugin_marketplaces"
    __table_args__ = (UniqueConstraint("org_id", "git_url", name="uq_plugin_marketplace_org_url"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(String(255))
    git_url: Mapped[str] = mapped_column(String(500))

    last_sync_status: Mapped[str] = mapped_column(String(20), default=MarketplaceStatus.OK.value)
    last_sync_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installations: Mapped[list[PluginInstallation]] = relationship(
        back_populates="marketplace",
        cascade="all, delete-orphan",
    )


class PluginInstallation(Base):
    """An installed plugin for an organization.

    Always sourced from a connected marketplace (``marketplace_id`` +
    ``plugin_name``). Clone coordinates are resolved at dispatch time from
    the live manifest so upgrades take effect without touching the DB.
    """

    __tablename__ = "plugin_installations"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "marketplace_id",
            "plugin_name",
            name="uq_plugin_install_marketplace",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    marketplace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plugin_marketplaces.id", ondelete="CASCADE"),
    )
    plugin_name: Mapped[str] = mapped_column(String(255))

    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    pinned_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled_workflows: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    project_overrides: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    marketplace: Mapped[PluginMarketplace] = relationship(back_populates="installations")
