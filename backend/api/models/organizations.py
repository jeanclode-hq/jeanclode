"""Organization and OrgMembership models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base


class Provider(StrEnum):
    """External service provider."""

    GITHUB = "github"
    GITLAB = "gitlab"
    SENTRY = "sentry"


class MemberRole(StrEnum):
    """Membership role."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class OnboardingStep(StrEnum):
    """Tenant onboarding step."""

    GIT_PROVIDER = "git_provider"
    SENTRY = "sentry"
    MAPPING = "mapping"
    COMPLETE = "complete"


class Organization(Base):
    """External organization — one per connected org/account on any provider."""

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint(
            "provider", "external_org_id", "base_url", name="uq_org_provider_external"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(50))
    external_org_id: Mapped[str] = mapped_column(String(255))
    installation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_token_encrypted: Mapped[str | None] = mapped_column(nullable=True)
    client_secret_encrypted: Mapped[str | None] = mapped_column(nullable=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    onboarding_step: Mapped[str] = mapped_column(
        String(50), default=OnboardingStep.GIT_PROVIDER.value
    )
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)

    # GitLab group/subgroup hierarchy — NULL for top-level or non-GitLab orgs.
    # CASCADE, not SET NULL: a subgroup org holds the projects of the group
    # that was connected and has no standing without it. Orphaning one leaves
    # a row indistinguishable from a group connected on purpose.
    parent_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    root_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    workspace: Mapped[Workspace] = relationship(  # noqa: F821
        back_populates="organizations"
    )
    memberships: Mapped[list[OrgMembership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    repositories: Mapped[list[Repository]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrgMembership(Base):
    """Links provider identities to organizations with a role."""

    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("org_id", "provider_identity_id", name="uq_org_membership"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    provider_identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_identities.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(50), default=MemberRole.MEMBER.value)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped[Organization] = relationship(back_populates="memberships")
    provider_identity: Mapped[ProviderIdentity] = relationship(  # noqa: F821
        back_populates="org_memberships"
    )
