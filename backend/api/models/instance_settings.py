"""InstanceSetting model — encrypted key-value store for instance-wide config."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class InstanceSetting(Base):
    """Encrypted key-value row for instance-wide settings.

    Rows are grouped by ``category`` (e.g. "github", "gitlab", "llm") and keyed
    by ``key`` (e.g. "client_id", "api_key"). All values are encrypted via
    ``DatabasePlugin.encrypt()`` before being stored.
    """

    __tablename__ = "instance_settings"
    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_instance_settings_category_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
