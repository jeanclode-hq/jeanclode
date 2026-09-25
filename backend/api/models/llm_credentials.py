"""LLMCredential model — ordered pool of LLM credentials with failover state.

Replaces the single ``llm`` instance-settings row (see
``docs/adr/010-llm-credential-failover-and-rate-limits.md``): an
admin-configured, priority-ordered list of credentials, each carrying its own
exhaustion state so dispatch can automatically fail over to the next one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class LLMCredentialKind(StrEnum):
    """Whether the credential is a pay-per-token API key or an OAuth subscription."""

    API_KEY = "api_key"
    OAUTH_SUBSCRIPTION = "oauth_subscription"


class LLMCredentialStatus(StrEnum):
    """Admission state for a credential row.

    ``stale`` means the credential hit a real 429 and is being skipped until
    ``stale_until`` — it is not a permanent removal, just a temporary skip in
    the priority walk.
    """

    ACTIVE = "active"
    STALE = "stale"


class LLMCredential(Base):
    """One credential in the admin-configured, priority-ordered LLM pool.

    ``priority`` is unique and admin-assigned; lower is tried first.
    ``secret_encrypted`` holds the API key or OAuth token, encrypted via
    ``DatabasePlugin.encrypt`` — same scheme as ``instance_settings``.
    """

    __tablename__ = "llm_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Informational only (pro | max | max_5x) — not used for admission control.
    plan_tier: Mapped[str | None] = mapped_column(String(50), nullable=True)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # How skills and issues refer to this credential when steering the fixer's LLM.
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    model_high: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Empty means triage never escalates the fixer to a heavier model.
    model_heavy: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    model_low: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=LLMCredentialStatus.ACTIVE.value
    )
    stale_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
