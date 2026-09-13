"""User model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base

if TYPE_CHECKING:
    from api.models.identities import ProviderIdentity


class User(Base):
    """Application user, linked to provider identities."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, unique=True)
    onboarding_step: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    identities: Mapped[list[ProviderIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    workspace_memberships: Mapped[list[WorkspaceMembership]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def display_username(self) -> str:
        """Return display username from first identity or email prefix."""
        for identity in self.identities:
            if identity.username:
                return identity.username
        if self.email:
            return self.email.split("@")[0]
        return f"user-{str(self.id)[:8]}"

    @property
    def avatar_url(self) -> str | None:
        """Return avatar URL from first identity that has one."""
        for identity in self.identities:
            if identity.avatar_url:
                return identity.avatar_url
        return None

    def get_identity(self, provider: str) -> ProviderIdentity | None:
        """Get the identity for a specific provider."""
        for identity in self.identities:
            if identity.provider == provider:
                return identity
        return None
