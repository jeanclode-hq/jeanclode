"""MemoryEntry model — workspace-scoped storage backing the agent memory tool."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class MemoryEntry(Base):
    """A single file-like entry in a workspace's agent memory store."""

    __tablename__ = "memory_entries"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="uq_memory_entry_path"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "metadata" collides with SQLAlchemy's declarative attribute namespace.
    entry_metadata: Mapped[dict | None] = mapped_column(
        "metadata_json", JSONB, nullable=True, default=None
    )
    content: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
