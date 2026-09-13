"""Plugin marketplace endpoints (6 routes).

* ``GET  /plugins``              — everything the UI needs in one call.
* ``POST /plugins``              — install a plugin from a connected marketplace.
* ``PATCH /plugins/{id}``        — update settings (pin, workflows, projects).
* ``DELETE /plugins/{id}``       — uninstall.
* ``POST /marketplaces``         — connect a marketplace.
* ``DELETE /marketplaces/{id}``  — disconnect (cascades installs).

Every plugin is installed from a marketplace. If a plugin is later removed
from its marketplace manifest, it stays in the DB (admin can see it) but is
silently dropped at dispatch time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import (
    db_create_marketplace,
    db_create_marketplace_install,
    db_delete_install,
    db_delete_marketplace,
    db_find_marketplace_install,
    db_get_credentials_by_org,
    db_get_installation_by_id,
    db_get_installations_by_org,
    db_get_marketplace_by_id,
    db_get_marketplace_by_url,
    db_get_marketplaces_by_org,
    db_get_org_by_id,
    db_update_install,
    db_update_marketplace_sync,
    get_session,
    run_in_session,
)
from api.models import User
from api.models.connectors import SubjectType
from api.models.plugins import MarketplaceStatus, PluginInstallation, PluginMarketplace
from api.routers.auth.dependencies import (
    get_current_user,
    verify_org_access,
    verify_org_access_from_body,
)
from api.routers.connectors.schemas import CredentialStatus
from api.routers.connectors.utils import credential_to_status

from .schemas import (
    ConnectMarketplaceRequest,
    InstalledPlugin,
    InstallPluginRequest,
    MarketplaceEntry,
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePluginEntry,
    PluginsOverview,
    UpdateInstallRequest,
)
from .utils import (
    get_marketplace_provider,
    gitlab_project_path,
    parse_github_url,
    parse_marketplace_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Plugins"])


class _OrgGitAuth(NamedTuple):
    """The org's git credentials, read out of the session it came from."""

    provider: str
    installation_id: str | None
    auth_token_encrypted: str | None


def read_org_git_auth(db: Session, org_id: uuid.UUID) -> _OrgGitAuth | None:
    org = db_get_org_by_id(db, org_id)
    if not org:
        return None
    return _OrgGitAuth(org.provider, org.installation_id, org.auth_token_encrypted)


async def resolve_org_git_token(
    auth: _OrgGitAuth | None, org_id: uuid.UUID, provider: str
) -> str | None:
    """Resolve the org's git token matching the given provider.

    Takes the credentials already read from the DB rather than a session: the
    GitHub branch makes an API call, and a session passed in here would hold
    its pooled connection for that whole round trip.
    """
    if not auth or auth.provider != provider:
        return None

    app = get_current_app()

    if provider == "github" and auth.installation_id and app.github:
        try:
            return await app.github.get_installation_access_token(auth.installation_id)
        except Exception:
            logger.warning("Failed to get GitHub installation token for org %s", org_id)
            return None

    if provider == "gitlab" and auth.auth_token_encrypted and app.database:
        try:
            return app.database.decrypt(auth.auth_token_encrypted)
        except Exception:
            logger.warning("Failed to decrypt GitLab token for org %s", org_id)
            return None

    return None


async def _fetch_manifest(git_url: str, *, auth_token: str | None = None) -> MarketplaceManifest:
    """Fetch marketplace.json from the right git plugin based on URL provider."""
    app = get_current_app()
    provider = get_marketplace_provider(git_url)

    if provider == "github":
        if not app.github:
            raise MarketplaceFetchError("GitHub plugin is not enabled")
        loc = parse_github_url(git_url)
        assert loc is not None
        status, text = await app.github.fetch_repo_file_text(
            loc.owner,
            loc.repo,
            ".claude-plugin/marketplace.json",
            ref=loc.ref,
            auth_token=auth_token,
        )
    else:
        if not app.gitlab:
            raise MarketplaceFetchError("GitLab plugin is not enabled")
        project_path = gitlab_project_path(git_url)
        if not project_path:
            raise MarketplaceFetchError(f"unrecognized git URL: {git_url}")
        status, text = await app.gitlab.fetch_repo_file_text(
            project_path,
            ".claude-plugin/marketplace.json",
            auth_token=auth_token,
        )

    return parse_marketplace_response(status, text, git_url)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_to_response(
    inst: PluginInstallation, credential: CredentialStatus | None = None
) -> InstalledPlugin:
    return InstalledPlugin(
        id=inst.id,
        org_id=inst.org_id,
        marketplace_id=inst.marketplace_id,
        plugin_name=inst.plugin_name,
        display_name=inst.display_name,
        description=inst.description,
        pinned_ref=inst.pinned_ref,
        enabled_workflows=inst.enabled_workflows,
        project_overrides=inst.project_overrides or {},
        credential=credential,
    )


def _plugin_credentials(db: Session, org_id: uuid.UUID) -> dict[uuid.UUID, CredentialStatus]:
    """Every plugin-install credential in the org, keyed by install id."""
    return {
        c.subject_id: credential_to_status(c)
        for c in db_get_credentials_by_org(db, org_id)
        if c.subject_type == SubjectType.PLUGIN_INSTALLATION.value
    }


def _marketplace_entry(
    m: PluginMarketplace,
    manifest: MarketplaceManifest | None,
    installed_names: set[str],
) -> MarketplaceEntry:
    plugins: list[MarketplacePluginEntry] | None
    if manifest is None:
        plugins = None
    else:
        plugins = [
            MarketplacePluginEntry(
                name=p.name,
                description=p.description,
                version=p.version,
                installed=p.name in installed_names,
            )
            for p in manifest.plugins
        ]

    return MarketplaceEntry(
        id=m.id,
        org_id=m.org_id,
        name=m.name,
        git_url=m.git_url,
        last_sync_status=m.last_sync_status,
        last_sync_error=m.last_sync_error,
        last_synced_at=m.last_synced_at,
        plugins=plugins,
    )


# ---------------------------------------------------------------------------
# GET /plugins
# ---------------------------------------------------------------------------


@router.get(
    "/plugins",
    operation_id="get_plugins_overview",
    response_model=PluginsOverview,
)
async def get_plugins_overview(
    org_id: uuid.UUID = Query(..., description="Organization ID"),
    current_user: User = Depends(verify_org_access),
) -> PluginsOverview:
    """Return marketplaces (with manifests inlined) and installed plugins.

    Three steps rather than one: read what we need, fetch every manifest with
    no session open, then write the sync results back. Fetching a manifest is
    a call out to GitHub or GitLab, so doing it inside a session would keep a
    pooled connection checked out for the length of that round trip — once per
    marketplace, on a page the whole team loads.
    """

    def _read(
        db: Session,
    ) -> tuple[
        list[uuid.UUID],
        list[str],
        _OrgGitAuth | None,
        list[InstalledPlugin],
        dict[uuid.UUID, set[str]],
    ]:
        marketplaces = db_get_marketplaces_by_org(db, org_id)
        installs = db_get_installations_by_org(db, org_id)
        credentials = _plugin_credentials(db, org_id)
        installed_by_market: dict[uuid.UUID, set[str]] = {}
        for inst in installs:
            installed_by_market.setdefault(inst.marketplace_id, set()).add(inst.plugin_name)
        return (
            [m.id for m in marketplaces],
            [m.git_url for m in marketplaces],
            read_org_git_auth(db, org_id),
            [_install_to_response(i, credentials.get(i.id)) for i in installs],
            installed_by_market,
        )

    market_ids, git_urls, org_auth, installed, installed_by_market = await run_in_session(_read)

    async def _fetch_one(git_url: str) -> MarketplaceManifest | MarketplaceFetchError:
        try:
            provider = get_marketplace_provider(git_url)
            token = await resolve_org_git_token(org_auth, org_id, provider)
            return await _fetch_manifest(git_url, auth_token=token)
        except MarketplaceFetchError as e:
            return e

    results = await asyncio.gather(*(_fetch_one(url) for url in git_urls))

    def _write(db: Session) -> list[MarketplaceEntry]:
        entries: list[MarketplaceEntry] = []
        for market_id, result in zip(market_ids, results, strict=True):
            m = db_get_marketplace_by_id(db, market_id)
            if not m:
                continue  # disconnected while we were fetching
            if isinstance(result, MarketplaceFetchError):
                db_update_marketplace_sync(db, m, status=MarketplaceStatus.ERROR, error=str(result))
                manifest = None
            else:
                db_update_marketplace_sync(
                    db, m, status=MarketplaceStatus.OK, synced_at=datetime.now(UTC)
                )
                manifest = result
            entries.append(
                _marketplace_entry(m, manifest, installed_by_market.get(market_id, set()))
            )
        return entries

    return PluginsOverview(
        marketplaces=await run_in_session(_write),
        installed=installed,
    )


# ---------------------------------------------------------------------------
# POST /plugins — install from marketplace
# ---------------------------------------------------------------------------


@router.post(
    "/plugins",
    operation_id="install_plugins",
    response_model=list[InstalledPlugin],
    status_code=201,
)
async def install_plugins(
    request: InstallPluginRequest,
    current_user: User = Depends(get_current_user),
) -> list[InstalledPlugin]:
    """Install one or more plugins from a connected marketplace."""

    def _read(db: Session) -> tuple[str, _OrgGitAuth | None]:
        verify_org_access_from_body(db, current_user, request.org_id)
        marketplace = db_get_marketplace_by_id(db, request.marketplace_id)
        if not marketplace or marketplace.org_id != request.org_id:
            raise HTTPException(status_code=404, detail="Marketplace not found")
        return marketplace.git_url, read_org_git_auth(db, request.org_id)

    git_url, org_auth = await run_in_session(_read)

    provider = get_marketplace_provider(git_url)
    auth_token = await resolve_org_git_token(org_auth, request.org_id, provider)
    try:
        manifest = await _fetch_manifest(git_url, auth_token=auth_token)
    except MarketplaceFetchError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    def _write(db: Session) -> list[InstalledPlugin]:
        manifest_by_name = {p.name: p for p in manifest.plugins}
        created: list[InstalledPlugin] = []

        for name in request.plugin_names:
            entry = manifest_by_name.get(name)
            if not entry:
                continue  # silently skip plugins no longer in manifest

            if db_find_marketplace_install(
                db,
                org_id=request.org_id,
                marketplace_id=request.marketplace_id,
                plugin_name=name,
            ):
                continue  # already installed, skip

            install = db_create_marketplace_install(
                db,
                org_id=request.org_id,
                marketplace_id=request.marketplace_id,
                plugin_name=entry.name,
                display_name=entry.name,
                description=entry.description,
                pinned_ref=request.pinned_ref,
            )
            created.append(_install_to_response(install))

        return created

    return await run_in_session(_write)


# ---------------------------------------------------------------------------
# PATCH / DELETE plugin
# ---------------------------------------------------------------------------


@router.patch(
    "/plugins/{install_id}",
    operation_id="update_installed_plugin",
    response_model=InstalledPlugin,
)
def update_installed_plugin(
    install_id: uuid.UUID,
    request: UpdateInstallRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> InstalledPlugin:
    """Patch settings on an installed plugin."""
    install = db_get_installation_by_id(db, install_id)
    if not install:
        raise HTTPException(status_code=404, detail="Plugin installation not found")
    verify_org_access_from_body(db, current_user, install.org_id)
    payload = {k: v for k, v in request.model_dump(exclude_unset=True).items() if v is not None}
    install = db_update_install(db, install, **payload)
    return _install_to_response(install)


@router.delete(
    "/plugins/{install_id}",
    operation_id="uninstall_plugin",
    status_code=204,
)
def uninstall_plugin(
    install_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    """Uninstall a plugin."""
    install = db_get_installation_by_id(db, install_id)
    if not install:
        raise HTTPException(status_code=404, detail="Plugin installation not found")
    verify_org_access_from_body(db, current_user, install.org_id)
    db_delete_install(db, install)


# ---------------------------------------------------------------------------
# POST / DELETE marketplace
# ---------------------------------------------------------------------------


@router.post(
    "/marketplaces",
    operation_id="connect_marketplace",
    response_model=MarketplaceEntry,
    status_code=201,
)
async def connect_marketplace(
    request: ConnectMarketplaceRequest,
    current_user: User = Depends(get_current_user),
) -> MarketplaceEntry:
    """Connect a marketplace by fetching and validating its manifest."""

    def _read(db: Session) -> _OrgGitAuth | None:
        verify_org_access_from_body(db, current_user, request.org_id)
        if db_get_marketplace_by_url(db, request.org_id, request.git_url):
            raise HTTPException(status_code=409, detail="This marketplace is already connected")
        return read_org_git_auth(db, request.org_id)

    org_auth = await run_in_session(_read)

    provider = get_marketplace_provider(request.git_url)
    auth_token = await resolve_org_git_token(org_auth, request.org_id, provider)
    try:
        manifest = await _fetch_manifest(request.git_url, auth_token=auth_token)
    except MarketplaceFetchError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    def _write(db: Session) -> MarketplaceEntry:
        marketplace = db_create_marketplace(
            db, org_id=request.org_id, name=manifest.name, git_url=request.git_url
        )
        db_update_marketplace_sync(
            db, marketplace, status=MarketplaceStatus.OK, synced_at=datetime.now(UTC)
        )
        return _marketplace_entry(marketplace, manifest, installed_names=set())

    return await run_in_session(_write)


@router.delete(
    "/marketplaces/{marketplace_id}",
    operation_id="disconnect_marketplace",
    status_code=204,
)
def disconnect_marketplace(
    marketplace_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    """Disconnect a marketplace and uninstall every plugin sourced from it."""
    marketplace = db_get_marketplace_by_id(db, marketplace_id)
    if not marketplace:
        raise HTTPException(status_code=404, detail="Marketplace not found")
    verify_org_access_from_body(db, current_user, marketplace.org_id)
    db_delete_marketplace(db, marketplace)
