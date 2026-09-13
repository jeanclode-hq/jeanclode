"""Schemas for the plugin marketplace endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from api.routers.connectors.schemas import CredentialStatus

# ---------------------------------------------------------------------------
# Internal models (manifest parsing, URL parsing, dispatch)
# ---------------------------------------------------------------------------


class MarketplacePlugin(BaseModel):
    """A single plugin entry inside marketplace.json."""

    name: str = Field(description="Plugin identifier")
    description: str | None = Field(default=None)
    version: str | None = Field(default=None)
    source: dict | str | None = Field(default=None)
    skills: list[str] | None = Field(
        default=None,
        description=(
            "Skill folder allowlist, relative to source, for plugins that share "
            "a source root with sibling plugins instead of owning a dedicated "
            ".claude-plugin/plugin.json."
        ),
    )


class MarketplaceManifest(BaseModel):
    """Parsed marketplace.json."""

    name: str = Field(description="Marketplace display name")
    description: str | None = Field(default=None)
    plugins: list[MarketplacePlugin] = Field(default_factory=list)


class GitHubLocator(BaseModel):
    """A parsed GitHub repo URL."""

    owner: str
    repo: str
    ref: str | None = None


class ResolvedPluginSpec(BaseModel):
    """A clone-ready third-party plugin pointer for the CLI worker."""

    git_url: str
    ref: str | None = None
    plugin_subpath: str | None = None
    display_name: str | None = None
    skills: list[str] | None = None


class MarketplaceFetchError(Exception):
    """Failed to fetch or parse a marketplace manifest."""


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class ConnectMarketplaceRequest(BaseModel):
    """Connect a new marketplace to an organization."""

    org_id: uuid.UUID
    git_url: str = Field(description="Git repo URL hosting marketplace.json")


class MarketplacePluginEntry(BaseModel):
    """A plugin advertised in a marketplace, plus install state for the org."""

    name: str
    description: str | None = None
    version: str | None = None
    installed: bool = False


class MarketplaceEntry(BaseModel):
    """Connected marketplace with its manifest contents inlined."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    git_url: str
    last_sync_status: str
    last_sync_error: str | None = None
    last_synced_at: datetime | None = None
    plugins: list[MarketplacePluginEntry] | None = None


class InstallPluginRequest(BaseModel):
    """Install one or more plugins from a connected marketplace."""

    org_id: uuid.UUID
    marketplace_id: uuid.UUID
    plugin_names: list[str] = Field(min_length=1)
    pinned_ref: str | None = None


class UpdateInstallRequest(BaseModel):
    """Patch fields on an installed plugin."""

    pinned_ref: str | None = None
    enabled_workflows: list[str] | None = Field(
        default=None,
        description="Workflows this plugin runs in. Null/empty = all workflows.",
    )
    project_overrides: dict | None = None


class InstalledPlugin(BaseModel):
    """An installed plugin as returned by the API."""

    id: uuid.UUID
    org_id: uuid.UUID
    marketplace_id: uuid.UUID
    plugin_name: str
    display_name: str
    description: str | None = None
    pinned_ref: str | None = None
    enabled_workflows: list[str] | None = None
    project_overrides: dict = Field(default_factory=dict)
    credential: CredentialStatus | None = Field(
        default=None,
        description="This install's stored auth, if any — inlined so the plugin "
        "list needs one request rather than one per installed plugin",
    )


class PluginsOverview(BaseModel):
    """Everything the UI needs in one call."""

    marketplaces: list[MarketplaceEntry]
    installed: list[InstalledPlugin]
