"""Service layer for the admin-configured LLM credential pool (ADR-010).

Thin encrypt/decrypt wrapper around ``api.database.llm_credentials``, mirroring
the ``instance_settings`` service's encryption pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database.llm_credentials import (
    db_create_llm_credential,
    db_delete_llm_credential,
    db_list_llm_credentials,
    db_reorder_llm_credentials,
    db_update_llm_credential,
)
from api.models.llm_credentials import LLMCredential


def _encrypt(value: str) -> str:
    db = get_current_app().database
    assert db is not None, "Database plugin is required for LLM credentials"
    return db.encrypt(value)


def _decrypt(value: str) -> str:
    db = get_current_app().database
    assert db is not None, "Database plugin is required for LLM credentials"
    return db.decrypt(value)


def list_llm_credentials(db: Session) -> list[LLMCredential]:
    """List every credential, ordered by priority. Secrets stay encrypted."""
    return db_list_llm_credentials(db)


def create_llm_credential(
    db: Session,
    *,
    kind: str,
    provider: str,
    secret: str,
    model_high: str = "",
    model_low: str = "",
    base_url: str | None = None,
    plan_tier: str | None = None,
) -> LLMCredential:
    """Encrypt ``secret`` and append a new credential to the pool."""
    return db_create_llm_credential(
        db,
        kind=kind,
        provider=provider,
        secret_encrypted=_encrypt(secret),
        model_high=model_high,
        model_low=model_low,
        base_url=base_url,
        plan_tier=plan_tier,
    )


def update_llm_credential(
    db: Session,
    credential_id: UUID,
    *,
    kind: str | None = None,
    provider: str | None = None,
    secret: str | None = None,
    model_high: str | None = None,
    model_low: str | None = None,
    base_url: str | None = None,
    plan_tier: str | None = None,
) -> LLMCredential | None:
    """Update fields on a credential. An empty/None ``secret`` leaves the
    existing encrypted value in place (rotate-only-if-provided, same
    convention as the instance-settings admin forms)."""
    return db_update_llm_credential(
        db,
        credential_id,
        kind=kind,
        provider=provider,
        secret_encrypted=_encrypt(secret) if secret else None,
        model_high=model_high,
        model_low=model_low,
        base_url=base_url,
        plan_tier=plan_tier,
    )


def delete_llm_credential(db: Session, credential_id: UUID) -> bool:
    """Remove a credential from the pool."""
    return db_delete_llm_credential(db, credential_id)


def reorder_llm_credentials(db: Session, ordered_ids: list[UUID]) -> list[LLMCredential]:
    """Reassign priority 1..N following ``ordered_ids``."""
    return db_reorder_llm_credentials(db, ordered_ids)


def decrypt_secret(credential: LLMCredential) -> str:
    """Decrypt a credential's secret for use in dispatch."""
    return _decrypt(credential.secret_encrypted)


def stale_until_from_retry_after(retry_after_seconds: float | None) -> datetime:
    """5h fallback when no ``retry-after`` header — the shorter of Anthropic's
    two known rolling-window sizes (5h/7d). Guessing short and being wrong is
    cheap (one more 429 corrects it); guessing long and being wrong idles a
    working credential for up to a week for nothing.
    """
    delta = timedelta(seconds=retry_after_seconds) if retry_after_seconds else timedelta(hours=5)
    return datetime.now(UTC) + delta
