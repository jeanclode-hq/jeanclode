"""Database operations for InstanceSetting model."""

from sqlalchemy.orm import Session

from api.models.instance_settings import InstanceSetting

GIT_PROVIDER_CATEGORIES = ("github", "gitlab")


def db_get_settings_by_category(db: Session, category: str) -> list[InstanceSetting]:
    """Return all settings for a category."""
    return db.query(InstanceSetting).filter(InstanceSetting.category == category).all()


def db_get_setting(db: Session, category: str, key: str) -> InstanceSetting | None:
    """Return a single setting row by category+key, or None."""
    return (
        db.query(InstanceSetting)
        .filter(InstanceSetting.category == category, InstanceSetting.key == key)
        .first()
    )


def db_upsert_setting(
    db: Session, category: str, key: str, value_encrypted: str
) -> InstanceSetting:
    """Insert or update a single setting. Caller is responsible for committing."""
    existing = db_get_setting(db, category, key)
    if existing:
        existing.value_encrypted = value_encrypted
        db.flush()
        return existing

    setting = InstanceSetting(category=category, key=key, value_encrypted=value_encrypted)
    db.add(setting)
    db.flush()
    return setting


def db_insert_setting(
    db: Session, category: str, key: str, value_encrypted: str
) -> InstanceSetting:
    """Insert a single setting, never updating an existing row. Caller commits.

    Unlike :func:`db_upsert_setting`, a concurrent row for the same
    category+key surfaces as an ``IntegrityError`` on flush rather than
    being silently overwritten — the caller that must converge on one
    value (memory signing secret) relies on that.
    """
    setting = InstanceSetting(category=category, key=key, value_encrypted=value_encrypted)
    db.add(setting)
    db.flush()
    return setting


def db_delete_settings_by_category(db: Session, category: str) -> int:
    """Delete all rows for a category. Returns number of rows deleted."""
    count = (
        db.query(InstanceSetting)
        .filter(InstanceSetting.category == category)
        .delete(synchronize_session=False)
    )
    db.commit()
    return count


def db_has_any_git_provider(db: Session) -> bool:
    """Return True if any github/gitlab setting row exists."""
    return (
        db.query(InstanceSetting)
        .filter(InstanceSetting.category.in_(GIT_PROVIDER_CATEGORIES))
        .first()
        is not None
    )
