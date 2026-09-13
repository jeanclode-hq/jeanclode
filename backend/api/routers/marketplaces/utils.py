"""Marketplace router utilities.

Provider detection, manifest parsing, frontmatter sanitization, and the
dispatch-time resolver. All Pydantic models live in ``schemas.py``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse

from pydantic import ValidationError

from .schemas import (
    GitHubLocator,
    MarketplaceFetchError,
    MarketplaceManifest,
    ResolvedPluginSpec,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


_GITHUB_HOSTS = {"github.com", "www.github.com"}


def get_marketplace_provider(git_url: str) -> str:
    """Return ``'github'`` or ``'gitlab'`` based on the URL."""
    return "github" if parse_github_url(git_url) is not None else "gitlab"


def parse_github_url(git_url: str) -> GitHubLocator | None:
    """Parse a GitHub repo URL into ``(owner, repo, ref)``.

    Returns ``None`` for non-GitHub URLs.
    """
    git_url = git_url.strip()
    ssh_match = re.match(r"^git@([^:]+):([^/]+)/(.+?)(\.git)?$", git_url)
    if ssh_match:
        host, owner, repo, _ = ssh_match.groups()
        if host in _GITHUB_HOSTS:
            return GitHubLocator(owner=owner, repo=repo)
        return None

    parsed = urlparse(git_url)
    if parsed.hostname not in _GITHUB_HOSTS:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    ref: str | None = None
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        ref = parts[3]

    return GitHubLocator(owner=owner, repo=repo, ref=ref)


def gitlab_project_path(git_url: str) -> str | None:
    """Extract the ``namespace/project`` path from a GitLab-style git URL."""
    parsed = urlparse(git_url.rstrip("/").removesuffix(".git"))
    if not parsed.path:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    if len(parts) >= 4 and parts[-2] in ("tree", "blob"):
        parts = parts[:-2]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_marketplace_response(status: int, text: str, git_url: str) -> MarketplaceManifest:
    """Parse a raw HTTP response into a ``MarketplaceManifest``.

    Raises ``MarketplaceFetchError`` on non-200 status or invalid content.
    """
    if status == 404:
        raise MarketplaceFetchError(f".claude-plugin/marketplace.json not found at {git_url}")
    if status in (401, 403):
        raise MarketplaceFetchError(
            f"access denied fetching marketplace.json from {git_url} "
            f"(status {status}) — check the git token"
        )
    if status >= 400:
        raise MarketplaceFetchError(f"failed to fetch marketplace.json: HTTP {status}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise MarketplaceFetchError(f"marketplace.json is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise MarketplaceFetchError("marketplace.json must be a JSON object")

    if "plugins" not in data and isinstance(data.get("data"), dict):
        data = data["data"]

    try:
        return MarketplaceManifest.model_validate(data)
    except ValidationError as e:
        raise MarketplaceFetchError(f"marketplace.json failed validation: {e}") from e


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def _normalize_subpath(raw: str) -> str | None:
    """Strip a source path down to ``None`` for anything meaning "repo root".

    ``"./"`` and ``"."`` (both common ways to spell "this repo") only have
    their ``/`` stripped by a plain ``.strip("/")``, leaving a truthy ``"."``
    that would otherwise be treated as a real subdirectory name.
    """
    subpath = raw.strip("/")
    return None if subpath in ("", ".") else subpath


def resolve_plugin_clone_target(
    source: dict | str | None, marketplace_git_url: str
) -> tuple[str, str | None] | None:
    """Map a marketplace.json plugin ``source`` field to ``(git_url, subpath)``."""
    if source is None:
        return marketplace_git_url, None
    if isinstance(source, str):
        return marketplace_git_url, _normalize_subpath(source)
    if isinstance(source, dict):
        if source.get("type") == "github" and source.get("repo"):
            repo = str(source["repo"]).strip("/")
            url = f"https://github.com/{repo}"
            subpath = source.get("path")
            return url, _normalize_subpath(str(subpath)) if subpath else None
        nested = source.get("source")
        if isinstance(nested, str):
            return marketplace_git_url, _normalize_subpath(nested)
    return None


# ---------------------------------------------------------------------------
# Frontmatter sanitization
# ---------------------------------------------------------------------------


_DANGEROUS_FRONTMATTER_KEYS = frozenset(
    {
        "allowed-tools",
        "allowed_tools",
        "tools",
        "hooks",
        "preToolUse",
        "postToolUse",
        "userPromptSubmit",
        "stop",
    }
)


def sanitize_frontmatter_text(text: str) -> str:
    """Strip dangerous frontmatter keys and ``!``-prefixed shell directives."""
    text = _strip_dangerous_frontmatter_keys(text)
    text = _strip_bang_command_blocks(text)
    return text


def _strip_dangerous_frontmatter_keys(text: str) -> str:
    match = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", text, re.DOTALL)
    if not match:
        return text
    head, body, tail = match.groups()
    cleaned_lines: list[str] = []
    drop_until_next_top_level = False
    for raw_line in body.splitlines():
        is_top_level = bool(re.match(r"^[A-Za-z0-9_\-]+\s*:", raw_line))
        if is_top_level:
            key = raw_line.split(":", 1)[0].strip()
            if key in _DANGEROUS_FRONTMATTER_KEYS:
                drop_until_next_top_level = True
                continue
            drop_until_next_top_level = False
            cleaned_lines.append(raw_line)
        elif not drop_until_next_top_level:
            cleaned_lines.append(raw_line)
    new_body = "\n".join(cleaned_lines)
    return head + new_body + tail + text[match.end() :]


def _strip_bang_command_blocks(text: str) -> str:
    text = re.sub(r"!`[^`]*`", "", text)
    text = re.sub(r"```!\s*\n.*?\n```", "", text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# Dispatch resolver
# ---------------------------------------------------------------------------


class PluginInstallRow(NamedTuple):
    """One install, flattened out of the ORM so it outlives its session."""

    plugin_name: str
    display_name: str | None
    pinned_ref: str | None
    marketplace_git_url: str


def read_plugin_install_rows(
    db: Session, org_id: UUID, *, workflow: str | None = None
) -> list[PluginInstallRow]:
    """Read an org's installs (optionally scoped to one workflow) as plain rows.

    Resolving them into specs means refetching every marketplace manifest over
    the network, so the rows leave the session behind first — see
    :func:`resolve_plugin_specs`.
    """
    from api.database.plugins import db_get_installations_by_org, db_get_marketplace_by_id

    installs = db_get_installations_by_org(db, org_id)
    if workflow is not None:
        installs = [i for i in installs if _workflow_enabled(i.enabled_workflows, workflow)]

    rows: list[PluginInstallRow] = []
    for install in installs:
        marketplace = db_get_marketplace_by_id(db, install.marketplace_id)
        if not marketplace:
            continue
        rows.append(
            PluginInstallRow(
                plugin_name=install.plugin_name,
                display_name=install.display_name,
                pinned_ref=install.pinned_ref,
                marketplace_git_url=marketplace.git_url,
            )
        )
    return rows


async def resolve_plugin_specs(
    rows: list[PluginInstallRow], *, auth_token: str | None = None
) -> list[ResolvedPluginSpec]:
    """Turn install rows into clone-ready specs. Touches the network, not the DB."""
    from api.context import get_current_app

    app = get_current_app()
    manifest_cache: dict[str, MarketplaceManifest] = {}
    specs: list[ResolvedPluginSpec] = []

    for row in rows:
        git_url_source = row.marketplace_git_url
        manifest = manifest_cache.get(git_url_source)
        if manifest is None:
            try:
                status, text = await _fetch_marketplace_file(
                    app, git_url_source, auth_token=auth_token
                )
                manifest = parse_marketplace_response(status, text, git_url_source)
            except MarketplaceFetchError as e:
                logger.warning(
                    "Failed to refetch marketplace %s for dispatch: %s", git_url_source, e
                )
                continue
            manifest_cache[git_url_source] = manifest

        entry = next((p for p in manifest.plugins if p.name == row.plugin_name), None)
        if not entry:
            logger.warning(
                "Plugin %r no longer present in marketplace %s — skipping",
                row.plugin_name,
                git_url_source,
            )
            continue

        target = resolve_plugin_clone_target(entry.source, git_url_source)
        if target is None:
            continue
        git_url, plugin_subpath = target
        specs.append(
            ResolvedPluginSpec(
                git_url=git_url,
                ref=row.pinned_ref or None,
                plugin_subpath=plugin_subpath,
                display_name=row.display_name,
                skills=entry.skills,
            )
        )

    return specs


async def resolve_plugin_specs_for_org(
    db: Session,
    org_id: UUID,
    *,
    workflow: str | None = None,
    auth_token: str | None = None,
) -> list[ResolvedPluginSpec]:
    """Resolve an org's plugin installs into clone-ready specs.

    ``db`` is only touched before the first fetch, so callers must not hold it
    open around this call — read the rows and resolve them separately when the
    session is theirs to scope.
    """
    rows = read_plugin_install_rows(db, org_id, workflow=workflow)
    return await resolve_plugin_specs(rows, auth_token=auth_token)


async def _fetch_marketplace_file(
    app: object, git_url: str, *, auth_token: str | None
) -> tuple[int, str]:
    """Fetch marketplace.json via the right git plugin. Used by the resolver."""
    from api.app import Application

    assert isinstance(app, Application)
    provider = get_marketplace_provider(git_url)
    if provider == "github":
        if not app.github:
            raise MarketplaceFetchError("GitHub plugin is not enabled")
        loc = parse_github_url(git_url)
        assert loc is not None
        return await app.github.fetch_repo_file_text(
            loc.owner,
            loc.repo,
            ".claude-plugin/marketplace.json",
            ref=loc.ref,
            auth_token=auth_token,
        )
    if not app.gitlab:
        raise MarketplaceFetchError("GitLab plugin is not enabled")
    project_path = gitlab_project_path(git_url)
    if not project_path:
        raise MarketplaceFetchError(f"unrecognized git URL: {git_url}")
    return await app.gitlab.fetch_repo_file_text(
        project_path,
        ".claude-plugin/marketplace.json",
        auth_token=auth_token,
    )


def _workflow_enabled(enabled: list[str] | None, workflow: str) -> bool:
    if not enabled:
        return True
    return workflow in enabled
