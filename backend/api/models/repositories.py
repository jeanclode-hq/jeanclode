"""Repository model."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base


class MappingMethod(StrEnum):
    """How a repository was mapped to another repository."""

    CODE_MAPPING = "code_mapping"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class Repository(Base):
    """External project or repository linked to an organization."""

    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("org_id", "external_id", name="uq_repository_org_external"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    web_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider: Mapped[str] = mapped_column(String(50))
    provider_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_token_encrypted: Mapped[str | None] = mapped_column(nullable=True)
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped[Organization] = relationship(  # noqa: F821
        back_populates="repositories"
    )
    pull_requests: Mapped[list[PullRequest]] = relationship(  # noqa: F821
        back_populates="repository", cascade="all, delete-orphan"
    )
    issues: Mapped[list[Issue]] = relationship(  # noqa: F821
        back_populates="repository", cascade="all, delete-orphan"
    )

    # At-most-one cross-source mapping (Sentry/Linear project -> git repo).
    # ``uselist=False`` is safe here because the only rows a non-git-provider
    # repo ever owns are cross-source mappings, which the write path
    # (db_update_repository_mapping) upserts in place — never more than one.
    # Ordered by created_at so a git-provider repo that happens to also own
    # repo-group edges (see RepositoryMapping) gets a deterministic pick
    # instead of an arbitrary one; group edges are read via
    # ``group_mappings``, not this relationship.
    mapping: Mapped[RepositoryMapping | None] = relationship(
        "RepositoryMapping",
        primaryjoin="Repository.id == RepositoryMapping.repo_id",
        foreign_keys="RepositoryMapping.repo_id",
        order_by="RepositoryMapping.created_at",
        uselist=False,
        viewonly=True,
    )
    # All outgoing edges where this repo is the source — used for git<->git
    # repo-group lookups, where a repo can have many related repos.
    group_mappings: Mapped[list[RepositoryMapping]] = relationship(
        "RepositoryMapping",
        primaryjoin="Repository.id == RepositoryMapping.repo_id",
        foreign_keys="RepositoryMapping.repo_id",
        viewonly=True,
    )

    @property
    def mapped_repo_id(self) -> uuid.UUID | None:
        """Back-compat accessor: the cross-source mapping's target repo id, if any."""
        return self.mapping.mapped_repo_id if self.mapping else None

    @property
    def mapped_repo(self) -> Repository | None:
        """Back-compat accessor: the cross-source mapping's target repo, if any."""
        return self.mapping.mapped_repo if self.mapping else None

    @property
    def mapping_method(self) -> str | None:
        """Back-compat accessor: how the cross-source mapping was created, if any."""
        return self.mapping.mapping_method if self.mapping else None


class RepositoryMapping(Base):
    """Directed repo-to-repo edge.

    Covers two flavors on one table:

    - Cross-source (``repo_id`` is a Sentry/Linear/etc. project, ``mapped_repo_id``
      is the git repo it resolves to). At most one row per ``repo_id`` — the
      write path upserts in place rather than the schema enforcing it, since a
      DB-level unique index on ``repo_id`` alone would break the second flavor.
    - Repo-to-repo grouping (both sides are git repos, ``mapping_method`` is
      always ``manual``). A repo can have many outgoing edges here. The write
      path canonicalizes pair order before insert so (A, B) and (B, A) never
      coexist as duplicate representations of the same undirected link.

    ``mapped_repo_id`` is always a git-provider (github/gitlab) repo, since
    only git repos are ever cloned — ``repo_id`` can be any provider.
    """

    __tablename__ = "repository_mappings"
    __table_args__ = (
        UniqueConstraint("repo_id", "mapped_repo_id", name="uq_repository_mapping_pair"),
        Index("ix_repository_mappings_repo_id", "repo_id"),
        # The composite unique constraint can't serve a ``mapped_repo_id = X``
        # lookup, and the Sentry dispatch queries all join / filter on the git
        # (mapped) side — Issue → Sentry repo → mapping → git repo → git org.
        # (Both indexes are created by the 2026-07-27 migration; declared here
        # so the model matches the DB and tests get the same plan as prod.)
        Index("ix_repository_mappings_mapped_repo_id", "mapped_repo_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    mapped_repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True
    )
    mapping_method: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repo: Mapped[Repository] = relationship(foreign_keys=[repo_id], viewonly=True)
    mapped_repo: Mapped[Repository | None] = relationship(
        foreign_keys=[mapped_repo_id], viewonly=True
    )
