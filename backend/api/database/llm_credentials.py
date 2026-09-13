"""Database operations for the LLMCredential pool (ADR-010)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from api.models.llm_credentials import LLMCredential, LLMCredentialStatus


def db_list_llm_credentials(db: Session) -> list[LLMCredential]:
    """Return every credential, ordered by priority (lower tried first)."""
    return db.query(LLMCredential).order_by(LLMCredential.priority).all()


def db_get_llm_credential(db: Session, credential_id: UUID) -> LLMCredential | None:
    """Return a single credential by id, or ``None``."""
    return db.query(LLMCredential).filter(LLMCredential.id == credential_id).first()


def db_next_llm_credential_priority(db: Session) -> int:
    """Return the next free priority slot (max existing + 1, or 1)."""
    current_max = db.query(func.max(LLMCredential.priority)).scalar()
    return (current_max or 0) + 1


def db_create_llm_credential(
    db: Session,
    *,
    kind: str,
    provider: str,
    secret_encrypted: str,
    model_high: str = "",
    model_low: str = "",
    base_url: str | None = None,
    plan_tier: str | None = None,
    priority: int | None = None,
) -> LLMCredential:
    """Insert a new credential. Appends to the end of the priority order
    unless an explicit ``priority`` is given."""
    credential = LLMCredential(
        priority=priority if priority is not None else db_next_llm_credential_priority(db),
        kind=kind,
        provider=provider,
        plan_tier=plan_tier,
        secret_encrypted=secret_encrypted,
        model_high=model_high,
        model_low=model_low,
        base_url=base_url,
        status=LLMCredentialStatus.ACTIVE.value,
    )
    db.add(credential)
    db.commit()
    db.refresh(credential)
    return credential


def db_update_llm_credential(
    db: Session,
    credential_id: UUID,
    **fields: object,
) -> LLMCredential | None:
    """Update the given fields on a credential row. Ignores ``None`` values
    for ``secret_encrypted`` so a rotate-only-if-provided admin PUT doesn't
    overwrite the existing encrypted secret with nothing."""
    credential = db_get_llm_credential(db, credential_id)
    if not credential:
        return None

    for key, value in fields.items():
        if key == "secret_encrypted" and not value:
            continue
        if value is not None and hasattr(credential, key):
            setattr(credential, key, value)

    db.commit()
    db.refresh(credential)
    return credential


def db_delete_llm_credential(db: Session, credential_id: UUID) -> bool:
    """Delete a credential by id. Returns True if a row was deleted."""
    count = db.query(LLMCredential).filter(LLMCredential.id == credential_id).delete()
    db.commit()
    return bool(count)


def db_reorder_llm_credentials(db: Session, ordered_ids: list[UUID]) -> list[LLMCredential]:
    """Reassign priority 1..N following ``ordered_ids``.

    Every existing credential must be present in ``ordered_ids`` — this is
    a full reorder, not a partial patch. Two passes avoid transient
    collisions with the ``priority`` unique constraint (shifting everything
    to a disjoint range first, then down to 1..N).
    """
    credentials = {c.id: c for c in db_list_llm_credentials(db)}
    if set(credentials) != set(ordered_ids):
        raise ValueError("ordered_ids must contain exactly the existing credential ids")

    offset = len(ordered_ids) + 1
    for idx, credential_id in enumerate(ordered_ids):
        credentials[credential_id].priority = offset + idx
    db.flush()

    for idx, credential_id in enumerate(ordered_ids):
        credentials[credential_id].priority = idx + 1
    db.commit()

    return db_list_llm_credentials(db)


class LLMCredentialAvailability(StrEnum):
    """Result of walking the credential pool at dispatch time."""

    AVAILABLE = "available"
    # No rows at all — an admin misconfiguration, not exhaustion. Never
    # retryable by waiting.
    NONE_CONFIGURED = "none_configured"
    # At least one row exists, but every one is currently stale.
    ALL_STALE = "all_stale"


def db_select_llm_credential(
    db: Session,
) -> tuple[LLMCredentialAvailability, LLMCredential | None, datetime | None]:
    """Walk the pool in priority order and pick the first usable credential.

    Returns ``(availability, credential, retry_at)``:
      * ``AVAILABLE`` — ``credential`` is the first non-stale row (or a
        stale row whose ``stale_until`` has already elapsed).
      * ``NONE_CONFIGURED`` — the table is empty; ``retry_at`` is ``None``
        since there is no ``stale_until`` to compute one from.
      * ``ALL_STALE`` — every row is stale; ``retry_at`` is the earliest
        ``stale_until`` across them.
    """
    rows = db_list_llm_credentials(db)
    if not rows:
        return LLMCredentialAvailability.NONE_CONFIGURED, None, None

    now = datetime.now(UTC)
    stale_untils: list[datetime] = []
    for row in rows:
        is_stale = row.status == LLMCredentialStatus.STALE.value and (
            row.stale_until is not None and row.stale_until > now
        )
        if not is_stale:
            return LLMCredentialAvailability.AVAILABLE, row, None
        stale_untils.append(row.stale_until)

    return LLMCredentialAvailability.ALL_STALE, None, min(stale_untils)


def db_mark_llm_credential_stale(
    db: Session,
    credential_id: UUID,
    *,
    stale_until: datetime,
) -> LLMCredential | None:
    """Mark a credential stale until ``stale_until``.

    Idempotent by design — a second 429 on an already-stale row simply
    overwrites ``stale_until`` with the fresher value (e.g. corrected from
    a real ``retry-after`` after an earlier 5-hour default guess).
    """
    credential = db_get_llm_credential(db, credential_id)
    if not credential:
        return None
    credential.status = LLMCredentialStatus.STALE.value
    credential.stale_until = stale_until
    db.commit()
    db.refresh(credential)
    return credential
