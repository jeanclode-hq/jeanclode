"""Database operations for ProviderIdentity model."""

from uuid import UUID

from sqlalchemy.orm import Session

from api.models.identities import ProviderIdentity


def db_get_or_create_provider_identity(
    db: Session,
    provider: str,
    external_id: str,
    username: str | None = None,
    avatar_url: str | None = None,
    user_id: UUID | None = None,
) -> tuple[ProviderIdentity, bool]:
    """Get or create a provider identity.

    Returns:
        Tuple of (ProviderIdentity, is_new)
    """
    identity = (
        db.query(ProviderIdentity)
        .filter(
            ProviderIdentity.provider == provider,
            ProviderIdentity.external_id == external_id,
        )
        .first()
    )

    if identity:
        if username is not None:
            identity.username = username
        if avatar_url is not None:
            identity.avatar_url = avatar_url
        if user_id is not None and identity.user_id is None:
            identity.user_id = user_id
        db.commit()
        db.refresh(identity)
        return identity, False

    identity = ProviderIdentity(
        provider=provider,
        external_id=external_id,
        username=username,
        avatar_url=avatar_url,
        user_id=user_id,
    )
    db.add(identity)
    db.commit()
    db.refresh(identity)
    return identity, True


def db_get_identity_by_external_id(
    db: Session,
    provider: str,
    external_id: str,
) -> ProviderIdentity | None:
    """Get identity by provider and external ID."""
    return (
        db.query(ProviderIdentity)
        .filter(
            ProviderIdentity.provider == provider,
            ProviderIdentity.external_id == external_id,
        )
        .first()
    )


def db_count_user_identities(db: Session, user_id: UUID) -> int:
    """Count the number of linked identities for a user."""
    return db.query(ProviderIdentity).filter(ProviderIdentity.user_id == user_id).count()


def db_unlink_identity_from_user(db: Session, user_id: UUID, provider: str) -> bool:
    """Unlink a provider identity from a user (sets user_id to None)."""
    identity = (
        db.query(ProviderIdentity)
        .filter(
            ProviderIdentity.user_id == user_id,
            ProviderIdentity.provider == provider,
        )
        .first()
    )

    if not identity:
        return False

    identity.user_id = None
    db.commit()
    return True
