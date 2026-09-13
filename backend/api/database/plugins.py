"""Database operations for plugin marketplaces and installations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from api.models.connectors import Credential
from api.models.plugins import MarketplaceStatus, PluginInstallation, PluginMarketplace

# ---------------------------------------------------------------------------
# PluginMarketplace
# ---------------------------------------------------------------------------


def db_get_marketplaces_by_org(db: Session, org_id: UUID) -> list[PluginMarketplace]:
    return (
        db.query(PluginMarketplace)
        .filter(PluginMarketplace.org_id == org_id)
        .order_by(PluginMarketplace.name)
        .all()
    )


def db_get_marketplace_by_id(db: Session, marketplace_id: UUID) -> PluginMarketplace | None:
    return db.query(PluginMarketplace).filter(PluginMarketplace.id == marketplace_id).first()


def db_get_marketplace_by_url(db: Session, org_id: UUID, git_url: str) -> PluginMarketplace | None:
    return (
        db.query(PluginMarketplace)
        .filter(PluginMarketplace.org_id == org_id, PluginMarketplace.git_url == git_url)
        .first()
    )


def db_create_marketplace(
    db: Session,
    *,
    org_id: UUID,
    name: str,
    git_url: str,
) -> PluginMarketplace:
    marketplace = PluginMarketplace(org_id=org_id, name=name, git_url=git_url)
    db.add(marketplace)
    db.commit()
    db.refresh(marketplace)
    return marketplace


def db_update_marketplace_sync(
    db: Session,
    marketplace: PluginMarketplace,
    *,
    status: MarketplaceStatus,
    synced_at: object | None = None,
    error: str | None = None,
) -> PluginMarketplace:
    marketplace.last_sync_status = status.value
    marketplace.last_sync_error = error
    if synced_at is not None:
        marketplace.last_synced_at = synced_at  # type: ignore[assignment]
    db.commit()
    db.refresh(marketplace)
    return marketplace


def db_delete_marketplace(db: Session, marketplace: PluginMarketplace) -> None:
    # Installations cascade-delete at the DB level (marketplace_id FK), which
    # bypasses db_delete_install's own credential cleanup — do it here too so
    # no Credential row is left pointing at a subject_id that no longer exists.
    install_ids = [i.id for i in marketplace.installations]
    if install_ids:
        db.query(Credential).filter(
            Credential.subject_type == "plugin_installation",
            Credential.subject_id.in_(install_ids),
        ).delete(synchronize_session=False)
    db.delete(marketplace)
    db.commit()


# ---------------------------------------------------------------------------
# PluginInstallation
# ---------------------------------------------------------------------------


def db_get_installations_by_org(db: Session, org_id: UUID) -> list[PluginInstallation]:
    return (
        db.query(PluginInstallation)
        .filter(PluginInstallation.org_id == org_id)
        .order_by(PluginInstallation.display_name)
        .all()
    )


def db_get_installation_by_id(db: Session, install_id: UUID) -> PluginInstallation | None:
    return db.query(PluginInstallation).filter(PluginInstallation.id == install_id).first()


def db_find_marketplace_install(
    db: Session,
    *,
    org_id: UUID,
    marketplace_id: UUID,
    plugin_name: str,
) -> PluginInstallation | None:
    return (
        db.query(PluginInstallation)
        .filter(
            PluginInstallation.org_id == org_id,
            PluginInstallation.marketplace_id == marketplace_id,
            PluginInstallation.plugin_name == plugin_name,
        )
        .first()
    )


def db_create_marketplace_install(
    db: Session,
    *,
    org_id: UUID,
    marketplace_id: UUID,
    plugin_name: str,
    display_name: str,
    description: str | None = None,
    pinned_ref: str | None = None,
) -> PluginInstallation:
    install = PluginInstallation(
        org_id=org_id,
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        display_name=display_name,
        description=description,
        pinned_ref=pinned_ref,
    )
    db.add(install)
    db.commit()
    db.refresh(install)
    return install


def db_update_install(
    db: Session,
    install: PluginInstallation,
    **kwargs: object,
) -> PluginInstallation:
    for key, value in kwargs.items():
        setattr(install, key, value)
    db.commit()
    db.refresh(install)
    return install


def db_delete_install(db: Session, install: PluginInstallation) -> None:
    # No DB-level FK from Credential.subject_id (polymorphic across two
    # subject tables) — the cascade has to happen here explicitly.
    db.query(Credential).filter(
        Credential.subject_type == "plugin_installation", Credential.subject_id == install.id
    ).delete()
    db.delete(install)
    db.commit()
