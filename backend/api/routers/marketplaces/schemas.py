"""Schemas for the plugin marketplace endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
            "Skill folder allowlist, relative to the plugin root, for plugins "
            "that share a source root with sibling plugins instead of owning a "
            "dedicated .claude-plugin/plugin.json."
        ),
    )

    @field_validator("skills", mode="before")
    @classmethod
    def _skills_as_list(cls, value: object) -> object:
        # Claude Code accepts a single path as well as an array.
        return [value] if isinstance(value, str) else value


class MarketplaceManifest(BaseModel):
    """Parsed marketplace.json."""

    name: str = Field(description="Marketplace display name")
    description: str | None = Field(default=None)
    metadata: dict | None = Field(default=None)
    plugins: list[MarketplacePlugin] = Field(default_factory=list)
    kind: Literal["marketplace", "skills"] = Field(
        default="marketplace",
        exclude=True,
        description="'skills' when built from the repo's SKILL.md files rather than a marketplace.json",
    )

    @property
    def plugin_root(self) -> str | None:
        root = (self.metadata or {}).get("pluginRoot")
        return root if isinstance(root, str) else None


class RepoLocator(BaseModel):
    """A git repository on GitHub or GitLab, optionally narrowed to a ref and folder."""

    provider: Literal["github", "gitlab"]
    host: str
    project_path: str
    ref: str | None = None
    subpath: str | None = None

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.project_path}"

    @property
    def name(self) -> str:
        return self.project_path.rsplit("/", 1)[-1]

    @property
    def canonical_url(self) -> str:
        """The web URL this locator round-trips through, used as the stored key."""
        if not self.ref and not self.subpath:
            return self.clone_url
        tree = "tree" if self.provider == "github" else "-/tree"
        url = f"{self.clone_url}/{tree}/{self.ref or 'HEAD'}"
        return f"{url}/{self.subpath}" if self.subpath else url


class RepoAuth(BaseModel):
    """An org's git token and the host it belongs to — it is never sent anywhere else."""

    provider: Literal["github", "gitlab"]
    token: str | None = None
    base_url: str | None = None


class PluginCloneTarget(BaseModel):
    """Where one marketplace plugin's files live."""

    git_url: str
    subpath: str | None = None
    ref: str | None = None
    sha: str | None = None


class ResolvedPluginSpec(BaseModel):
    """A clone-ready third-party plugin pointer for the CLI worker."""

    git_url: str
    ref: str | None = None
    sha: str | None = None
    plugin_subpath: str | None = None
    display_name: str | None = None
    skills: list[str] | None = None


class MarketplaceFetchError(Exception):
    """Failed to fetch or parse a marketplace manifest."""


class UnsupportedPluginSourceError(ValueError):
    """A marketplace plugin ``source`` jeanclode can't fetch."""


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class ConnectMarketplaceRequest(BaseModel):
    """Connect a new marketplace to an organization."""

    org_id: uuid.UUID
    git_url: str = Field(
        description=(
            "Where the skills live: a GitHub `owner/repo`, or a GitHub/GitLab URL "
            "(clone URL, or web URL down to a branch, folder or SKILL.md). The repo "
            "is read as a marketplace when it has .claude-plugin/marketplace.json, "
            "otherwise every SKILL.md folder in it becomes an installable skill."
        )
    )


class MarketplacePluginEntry(BaseModel):
    """A plugin advertised in a marketplace, plus install state for the org."""

    name: str
    description: str | None = None
    version: str | None = None
    installed: bool = False
    unsupported_reason: str | None = Field(
        default=None,
        description="Why this plugin can't be installed, when its source type isn't supported",
    )


class MarketplaceEntry(BaseModel):
    """Connected marketplace with its manifest contents inlined."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    git_url: str
    kind: Literal["marketplace", "skills"] | None = Field(
        default=None,
        description="'skills' for a plain repo of SKILL.md folders; null when the source is unreachable",
    )
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
    credentials: list[CredentialStatus] = Field(
        default_factory=list,
        description="This install's stored auths — inlined so the plugin "
        "list needs one request rather than one per installed plugin",
    )


class PluginsOverview(BaseModel):
    """Everything the UI needs in one call."""

    marketplaces: list[MarketplaceEntry]
    installed: list[InstalledPlugin]
