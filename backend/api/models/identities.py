"""ProviderIdentity model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base


class ProviderIdentity(Base):
    """Links a User to a git provider account (GitHub/GitLab)."""

    __tablename__ = "provider_identities"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_provider_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped[User] = relationship(back_populates="identities")  # noqa: F821
    org_memberships: Mapped[list[OrgMembership]] = relationship(  # noqa: F821
        back_populates="provider_identity", cascade="all, delete-orphan"
    )
