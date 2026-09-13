"""Service layer for encrypted instance settings.

Translates structured provider config dicts to/from encrypted key-value rows
in ``instance_settings``. All values are round-tripped through
``DatabasePlugin.encrypt/decrypt`` (AES-256-GCM).
"""

from __future__ import annotations

import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database.instance_settings import (
    db_delete_settings_by_category,
    db_get_setting,
    db_get_settings_by_category,
    db_has_any_git_provider,
    db_insert_setting,
    db_upsert_setting,
)

GITHUB_KEYS = (
    "client_id",
    "client_secret",
    "app_id",
    "private_key_pem",
    "webhook_secret",
    "name",
    # Owner metadata captured from GitHub's manifest conversion response —
    # used by the admin UI to deep-link to the correct App settings page
    # (user vs org). Optional for the paste-credentials flow.
    "owner_login",
    "owner_type",
)
GITLAB_KEYS = ("client_id", "client_secret", "instance_url", "webhook_secret")


def _encrypt(value: str) -> str:
    db = get_current_app().database
    assert db is not None, "Database plugin is required for instance settings"
    return db.encrypt(value)


def _decrypt(value: str) -> str:
    db = get_current_app().database
    assert db is not None, "Database plugin is required for instance settings"
    return db.decrypt(value)


def _save_category(db: Session, category: str, config: dict[str, str | None]) -> None:
    """Upsert each key in ``config``; skip keys whose value is None or empty.

    Empty strings are treated as "leave unchanged" so partial admin updates
    (e.g. rotating only ``name`` without retyping masked secrets) don't
    overwrite existing encrypted values with blanks. Use
    ``delete_category`` to actually clear a category.
    """
    for key, value in config.items():
        if not value:
            continue
        db_upsert_setting(db, category, key, _encrypt(value))
    db.commit()


def _load_category(
    db: Session, category: str, expected_keys: tuple[str, ...]
) -> dict[str, str] | None:
    """Load rows for a category and decrypt them. Returns ``None`` if no rows."""
    rows = db_get_settings_by_category(db, category)
    if not rows:
        return None
    config: dict[str, str] = {}
    for row in rows:
        if row.key not in expected_keys:
            continue
        config[row.key] = _decrypt(row.value_encrypted)
    return config or None


def save_github_config(db: Session, config: dict[str, str | None]) -> None:
    """Persist GitHub App credentials (client_id, client_secret, app_id, private_key_pem, ...)."""
    _save_category(db, "github", {k: config.get(k) for k in GITHUB_KEYS})


def load_github_config(db: Session) -> dict[str, str] | None:
    """Load GitHub App credentials or ``None`` if not configured."""
    return _load_category(db, "github", GITHUB_KEYS)


def save_gitlab_config(db: Session, config: dict[str, str | None]) -> None:
    """Persist GitLab OAuth credentials."""
    _save_category(db, "gitlab", {k: config.get(k) for k in GITLAB_KEYS})


def load_gitlab_config(db: Session) -> dict[str, str] | None:
    """Load GitLab OAuth credentials or ``None`` if not configured."""
    return _load_category(db, "gitlab", GITLAB_KEYS)


def delete_category(db: Session, category: str) -> int:
    """Delete all rows for a category; returns rows deleted."""
    return db_delete_settings_by_category(db, category)


def has_any_git_provider(db: Session) -> bool:
    """Return True if any github/gitlab row exists in the DB."""
    return db_has_any_git_provider(db)


MEMORY_SIGNING_SECRET_KEY = "internal_signing_secret"


def get_or_create_memory_signing_secret(db: Session) -> str:
    """Return the HMAC secret for signing memory API tokens, generating and
    persisting one on first use so every backend replica converges on the
    same value instead of minting tokens no other pod can verify."""
    existing = db_get_setting(db, "memory", MEMORY_SIGNING_SECRET_KEY)
    if existing is not None:
        return _decrypt(existing.value_encrypted)

    secret = secrets.token_hex(32)
    try:
        # INSERT-only on purpose: db_upsert_setting would SELECT-then-UPDATE,
        # and a racer whose SELECT lands after the winner's commit would
        # overwrite the winning secret instead of raising here — leaving each
        # replica signing with a different value.
        db_insert_setting(db, "memory", MEMORY_SIGNING_SECRET_KEY, _encrypt(secret))
        db.commit()
    except IntegrityError:
        # Lost the race to another concurrent first-start; use theirs.
        db.rollback()
        existing = db_get_setting(db, "memory", MEMORY_SIGNING_SECRET_KEY)
        assert existing is not None
        return _decrypt(existing.value_encrypted)

    return secret
