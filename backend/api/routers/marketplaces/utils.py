"""Marketplace router utilities.

Plugin source resolution, frontmatter sanitization, and the dispatch-time
resolver. Reading a source repo lives in ``sources.py``. All Pydantic models live in ``schemas.py``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse

from .schemas import (
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
    PluginCloneTarget,
    RepoAuth,
    ResolvedPluginSpec,
    UnsupportedPluginSourceError,
)
from .sources import (
    InvalidSourceError,
    load_source_manifest,
    normalize_repo_path,
    parse_repo_locator,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def marketplace_clone_target(git_url: str) -> tuple[str, str | None]:
    """The clonable URL and ref behind a stored source URL.

    A source may be stored as its web link (``.../tree/<ref>/<folder>``),
    which ``git clone`` can't fetch, so relative-path plugins clone the bare
    repo at that ref instead.
    """
    try:
        loc = parse_repo_locator(git_url)
    except InvalidSourceError as e:
        raise UnsupportedPluginSourceError(str(e)) from e
    return loc.clone_url, loc.ref


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


_GITHUB_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_PLUGIN_SOURCES = ("github", "url", "git-subdir")
_SUPPORTED_SOURCES_HINT = "relative path, github, url or git-subdir"


def _normalize_subpath(raw: str) -> str | None:
    """A path inside the plugin's repo, ``None`` meaning its root."""
    try:
        return normalize_repo_path(raw)
    except InvalidSourceError as e:
        raise UnsupportedPluginSourceError(str(e)) from e


def _https_git_url(raw: str) -> str:
    """Rewrite an SSH git URL to HTTPS: the sandbox only reaches git through
    the HTTPS proxy, which is also where credentials are injected."""
    raw = raw.strip()
    scp = re.match(r"^[\w.-]+@([^:/]+):(.+)$", raw)
    if scp:
        return f"https://{scp.group(1)}/{scp.group(2).lstrip('/')}"
    parsed = urlparse(raw)
    if parsed.scheme == "ssh" and parsed.hostname:
        return f"https://{parsed.hostname}{parsed.path}"
    if parsed.scheme == "https" and parsed.hostname:
        return raw
    raise UnsupportedPluginSourceError(
        f"git URL {raw!r} is not supported — use https:// (or git@host:path)"
    )


def _git_pin(source: dict) -> tuple[str | None, str | None]:
    ref = source.get("ref")
    sha = source.get("sha")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise UnsupportedPluginSourceError("'ref' must be a non-empty string")
    if sha is not None and (not isinstance(sha, str) or not _SHA_RE.match(sha)):
        raise UnsupportedPluginSourceError("'sha' must be a full 40-character lowercase commit SHA")
    return (ref.strip() if ref else None), sha


def _relative_target(
    raw: str, marketplace_git_url: str, plugin_root: str | None
) -> PluginCloneTarget:
    if plugin_root and raw and "/" not in raw and raw not in (".", ".."):
        raw = f"{plugin_root.rstrip('/')}/{raw}"
    git_url, ref = marketplace_clone_target(marketplace_git_url)
    return PluginCloneTarget(git_url=git_url, subpath=_normalize_subpath(raw), ref=ref)


def resolve_plugin_clone_target(
    source: dict | str | None,
    marketplace_git_url: str,
    *,
    plugin_root: str | None = None,
) -> PluginCloneTarget:
    """Map a marketplace.json plugin ``source`` to the repo and folder to clone.

    Follows Claude Code's plugin source schema. Raises
    ``UnsupportedPluginSourceError`` for anything that can't be cloned with
    git (``npm``, ``archive``, ``command``) or is malformed.
    """
    if source is None:
        return _relative_target("", marketplace_git_url, plugin_root)
    if isinstance(source, str):
        return _relative_target(source, marketplace_git_url, plugin_root)
    if not isinstance(source, dict):
        raise UnsupportedPluginSourceError(f"source must be a string or object, got {source!r}")

    # ``type`` predates Claude Code's ``source`` key and stays for old manifests.
    kind = source.get("source", source.get("type"))
    if isinstance(kind, str) and kind not in _REMOTE_PLUGIN_SOURCES:
        if kind.startswith((".", "/")):
            return _relative_target(kind, marketplace_git_url, plugin_root)
        raise UnsupportedPluginSourceError(
            f"source type {kind!r} is not supported (supported: {_SUPPORTED_SOURCES_HINT})"
        )
    if kind is None:
        raise UnsupportedPluginSourceError("source object has no 'source' type")

    ref, sha = _git_pin(source)
    subpath = source.get("path")
    if subpath is not None and not isinstance(subpath, str):
        raise UnsupportedPluginSourceError("'path' must be a string")

    if kind == "github":
        repo = str(source.get("repo") or "").strip().strip("/").removesuffix(".git")
        if not _GITHUB_SHORTHAND_RE.match(repo):
            raise UnsupportedPluginSourceError(
                f"github source needs repo 'owner/repo', got {repo!r}"
            )
        git_url = f"https://github.com/{repo}"
    else:
        raw_url = source.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise UnsupportedPluginSourceError(f"{kind} source needs a 'url'")
        if kind == "git-subdir" and _GITHUB_SHORTHAND_RE.match(raw_url.strip()):
            git_url = f"https://github.com/{raw_url.strip()}"
        else:
            git_url = _https_git_url(raw_url)
        if kind == "git-subdir" and not (subpath and subpath.strip("./")):
            raise UnsupportedPluginSourceError("git-subdir source needs a 'path'")

    return PluginCloneTarget(
        git_url=git_url,
        subpath=_normalize_subpath(subpath) if subpath else None,
        ref=ref,
        sha=sha,
    )


def plugin_unsupported_reason(
    plugin: MarketplacePlugin, manifest: MarketplaceManifest, marketplace_git_url: str
) -> str | None:
    try:
        resolve_plugin_clone_target(
            plugin.source, marketplace_git_url, plugin_root=manifest.plugin_root
        )
    except UnsupportedPluginSourceError as e:
        return str(e)
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
    rows: list[PluginInstallRow], *, auth: RepoAuth | None = None
) -> list[ResolvedPluginSpec]:
    """Turn install rows into clone-ready specs. Touches the network, not the DB."""
    manifest_cache: dict[str, MarketplaceManifest] = {}
    specs: list[ResolvedPluginSpec] = []

    for row in rows:
        git_url_source = row.marketplace_git_url
        manifest = manifest_cache.get(git_url_source)
        if manifest is None:
            try:
                manifest = await load_source_manifest(git_url_source, auth=auth)
            except MarketplaceFetchError as e:
                logger.error(
                    "Skill source %s unreadable at dispatch, its plugins won't load: %s",
                    git_url_source,
                    e,
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

        try:
            target = resolve_plugin_clone_target(
                entry.source, git_url_source, plugin_root=manifest.plugin_root
            )
        except UnsupportedPluginSourceError as e:
            logger.error(
                "Plugin %r in marketplace %s has an unsupported source, not loading it: %s",
                row.plugin_name,
                git_url_source,
                e,
            )
            continue
        # An install pin overrides whatever the manifest pins.
        specs.append(
            ResolvedPluginSpec(
                git_url=target.git_url,
                ref=row.pinned_ref or target.ref,
                sha=None if row.pinned_ref else target.sha,
                plugin_subpath=target.subpath,
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
    auth: RepoAuth | None = None,
) -> list[ResolvedPluginSpec]:
    """Resolve an org's plugin installs into clone-ready specs.

    ``db`` is only touched before the first fetch, so callers must not hold it
    open around this call — read the rows and resolve them separately when the
    session is theirs to scope.
    """
    rows = read_plugin_install_rows(db, org_id, workflow=workflow)
    return await resolve_plugin_specs(rows, auth=auth)


def _workflow_enabled(enabled: list[str] | None, workflow: str) -> bool:
    if not enabled:
        return True
    return workflow in enabled
