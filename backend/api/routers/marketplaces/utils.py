"""Marketplace router utilities.

Provider detection, manifest parsing, frontmatter sanitization, and the
dispatch-time resolver. All Pydantic models live in ``schemas.py``.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse

from pydantic import ValidationError

from .schemas import (
    GitHubLocator,
    GitLabLocator,
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
    PluginCloneTarget,
    ResolvedPluginSpec,
    UnsupportedPluginSourceError,
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


def parse_gitlab_url(git_url: str) -> GitLabLocator | None:
    """Parse a GitLab repo URL into ``(project_path, ref)``.

    Understands the web UI's ``/-/tree/<ref>`` and ``/-/blob/<ref>/...`` forms
    as well as plain clone URLs (``https://`` or ``git@``).
    """
    git_url = git_url.strip()
    ssh_match = re.match(r"^git@[^:]+:(.+?)(\.git)?/?$", git_url)
    if ssh_match:
        path = ssh_match.group(1).strip("/")
        return GitLabLocator(project_path=path) if "/" in path else None

    path = urlparse(git_url).path
    ref: str | None = None
    if "/-/" in path:
        path, _, rest = path.partition("/-/")
        kind, _, ref_part = rest.partition("/")
        if kind == "tree" and ref_part:
            ref = ref_part.strip("/")
        elif kind == "blob" and ref_part:
            ref = ref_part.split("/", 1)[0]
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 4 and parts[-2] in ("tree", "blob"):
        ref = ref or parts[-1]
        parts = parts[:-2]
    if len(parts) < 2:
        return None
    return GitLabLocator(project_path="/".join(parts).removesuffix(".git"), ref=ref)


def gitlab_project_path(git_url: str) -> str | None:
    """Extract the ``namespace/project`` path from a GitLab-style git URL."""
    loc = parse_gitlab_url(git_url)
    return loc.project_path if loc else None


def marketplace_clone_target(git_url: str) -> tuple[str, str | None]:
    """The clonable URL and ref behind a marketplace URL.

    A marketplace may be connected by its web UI link (``.../tree/<ref>``),
    which ``git clone`` can't fetch, so relative-path plugins clone from the
    bare repo URL at that ref instead.
    """
    gh = parse_github_url(git_url)
    if gh is not None:
        return f"https://github.com/{gh.owner}/{gh.repo}", gh.ref
    parsed = urlparse(git_url.strip())
    gl = parse_gitlab_url(git_url)
    if gl is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return git_url, None
    return f"{parsed.scheme}://{parsed.netloc}/{gl.project_path}", gl.ref


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


_GITHUB_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_PLUGIN_SOURCES = ("github", "url", "git-subdir")
_SUPPORTED_SOURCES_HINT = "relative path, github, url or git-subdir"


def _normalize_subpath(raw: str) -> str | None:
    """Normalize a source path inside a repo, ``None`` meaning the repo root.

    Refuses ``..`` so a manifest can't point the clone outside its own repo.
    """
    subpath = posixpath.normpath("/" + raw.strip()).strip("/")
    if ".." in raw.replace("\\", "/").split("/"):
        raise UnsupportedPluginSourceError(f"path {raw!r} must not contain '..'")
    return subpath or None


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
    auth_token: str | None = None,
) -> list[ResolvedPluginSpec]:
    """Resolve an org's plugin installs into clone-ready specs.

    ``db`` is only touched before the first fetch, so callers must not hold it
    open around this call — read the rows and resolve them separately when the
    session is theirs to scope.
    """
    rows = read_plugin_install_rows(db, org_id, workflow=workflow)
    return await resolve_plugin_specs(rows, auth_token=auth_token)


async def fetch_marketplace_manifest(
    git_url: str, *, auth_token: str | None = None
) -> MarketplaceManifest:
    """Fetch and parse marketplace.json at the ref the URL names, if any."""
    from api.context import get_current_app

    status, text = await _fetch_marketplace_file(get_current_app(), git_url, auth_token=auth_token)
    return parse_marketplace_response(status, text, git_url)


async def _fetch_marketplace_file(
    app: object, git_url: str, *, auth_token: str | None
) -> tuple[int, str]:
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
    gl = parse_gitlab_url(git_url)
    if gl is None:
        raise MarketplaceFetchError(f"unrecognized git URL: {git_url}")
    return await app.gitlab.fetch_repo_file_text(
        gl.project_path,
        ".claude-plugin/marketplace.json",
        ref=gl.ref,
        auth_token=auth_token,
    )


def _workflow_enabled(enabled: list[str] | None, workflow: str) -> bool:
    if not enabled:
        return True
    return workflow in enabled
