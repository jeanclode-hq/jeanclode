"""Where skills come from: parse what a user pastes, read the repo, list its skills.

A source is one GitHub or GitLab repository, optionally narrowed to a ref and
a folder. It is read over the provider's REST API only — the backend never
clones and never runs anything a repo ships. A repo with
``.claude-plugin/marketplace.json`` is a Claude Code marketplace; any other
repo is scanned for ``SKILL.md`` folders, the layout ``npx skills add``,
Claude Code's ``.claude/skills`` and Codex skill repos all share.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import ValidationError

from api.context import get_current_app

from .schemas import (
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
    RepoAuth,
    RepoLocator,
)

if TYPE_CHECKING:
    from api.app import Application

logger = logging.getLogger(__name__)

MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
SKILL_FILE = "SKILL.md"

# Bounds on what one source may cost to read.
MAX_SKILLS = 200
MAX_DESCRIBED_SKILLS = 50
MAX_TREE_PAGES = 50
_DESCRIBE_CONCURRENCY = 8
_DESCRIPTION_MAX_CHARS = 300
DESCRIBED_CACHE_TTL_SECONDS = 300
_DESCRIBED_CACHE_MAX = 256
# Keyed by token hash too, so one org's view never serves another's private repo.
_described_cache: dict[tuple[str, str], tuple[float, MarketplaceManifest]] = {}

_GITHUB_HOSTS = {"github.com", "www.github.com"}
# GitHub owners are alphanumerics and hyphens, so ``gitlab.com/x`` is never one.
_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9-]+/[\w.-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$")
_SCP_RE = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")
_IGNORED_DIRS = frozenset({".git", "node_modules"})


class InvalidSourceError(MarketplaceFetchError):
    """What the user pasted isn't a source jeanclode can read."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_repo_locator(raw: str) -> RepoLocator:
    """Parse a pasted source into a :class:`RepoLocator`.

    Accepts ``owner/repo`` (GitHub, like ``npx skills add``), clone URLs
    (``https://``, ``ssh://``, ``git@host:path``), and web URLs down to a
    branch, folder or file (``/tree/<ref>/<path>``, ``/-/tree/<ref>/<path>``,
    ``.../blob/<ref>/<path>/SKILL.md``). In a web URL the ref is one path
    segment: a branch with a ``/`` in it can't be told apart from a folder.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidSourceError("source is empty")

    if "://" not in text and _HOST_RE.match(text.split("/", 1)[0]):
        text = f"https://{text}"

    if _SHORTHAND_RE.match(text):
        host, path = "github.com", text
    elif scp := _SCP_RE.match(text):
        host, path = scp.group(1).lower(), scp.group(2)
    else:
        parsed = urlparse(text)
        if parsed.scheme not in ("https", "ssh") or not parsed.hostname:
            raise InvalidSourceError(
                f"{text!r} is not a GitHub `owner/repo` or an https:// / ssh git URL"
            )
        if parsed.query or parsed.fragment:
            raise InvalidSourceError(f"{text!r} has a query or fragment — paste the plain URL")
        host = parsed.hostname.lower()
        if parsed.port and parsed.scheme == "https":
            host = f"{host}:{parsed.port}"
        path = parsed.path

    if host in _GITHUB_HOSTS:
        return _github_locator(path, text)
    return _gitlab_locator(host, path, text)


def _split_parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _github_locator(path: str, raw: str) -> RepoLocator:
    parts = _split_parts(path)
    if len(parts) < 2:
        raise InvalidSourceError(f"{raw!r} is missing the repository name")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    rest = parts[2:]
    ref, subpath = None, None
    if rest:
        if rest[0] not in ("tree", "blob") or len(rest) < 2:
            raise InvalidSourceError(f"{raw!r} is not a repository, tree or blob URL")
        ref, subpath = _ref_and_subpath(rest[0], rest[1], rest[2:], raw)
    return RepoLocator(
        provider="github",
        host="github.com",
        project_path=f"{owner}/{repo}",
        ref=ref,
        subpath=subpath,
    )


def _gitlab_locator(host: str, path: str, raw: str) -> RepoLocator:
    parts = _split_parts(path)
    ref, subpath = None, None
    if "-" in parts:
        idx = parts.index("-")
        project, rest = parts[:idx], parts[idx + 1 :]
        if len(rest) < 2 or rest[0] not in ("tree", "blob"):
            raise InvalidSourceError(f"{raw!r} is not a repository, tree or blob URL")
        ref, subpath = _ref_and_subpath(rest[0], rest[1], rest[2:], raw)
    else:
        project = parts
    if len(project) < 2:
        raise InvalidSourceError(f"{raw!r} is missing the group or project name")
    project[-1] = project[-1].removesuffix(".git")
    return RepoLocator(
        provider="gitlab",
        host=host,
        project_path="/".join(project),
        ref=ref,
        subpath=subpath,
    )


def _ref_and_subpath(
    kind: str, ref: str, rest: list[str], raw: str
) -> tuple[str | None, str | None]:
    if kind == "blob":
        if not rest:
            raise InvalidSourceError(f"{raw!r} doesn't name a file")
        if rest[-1] == SKILL_FILE:
            rest = rest[:-1]
        elif rest[-2:] == [".claude-plugin", "marketplace.json"]:
            rest = rest[:-2]
        else:
            raise InvalidSourceError(
                f"{raw!r} links a file — link a folder, a SKILL.md or marketplace.json"
            )
    subpath = normalize_repo_path("/".join(rest)) if rest else None
    return (None if ref == "HEAD" else ref), subpath


def normalize_repo_path(raw: str) -> str | None:
    """A path inside a repo, ``None`` meaning its root. Refuses ``..``."""
    if ".." in raw.replace("\\", "/").split("/"):
        raise InvalidSourceError(f"path {raw!r} must not contain '..'")
    return posixpath.normpath("/" + raw.strip()).strip("/") or None


# ---------------------------------------------------------------------------
# Repo access
# ---------------------------------------------------------------------------


def _gitlab_instance(app: Application, loc: RepoLocator, auth: RepoAuth | None) -> str:
    """The GitLab base URL to call for ``loc`` — only hosts jeanclode is connected to.

    Refusing other hosts keeps a pasted URL from making the backend call an
    arbitrary server.
    """
    candidates: list[str] = []
    if auth and auth.provider == "gitlab" and auth.base_url:
        candidates.append(auth.base_url)
    if app.gitlab:
        candidates.append(app.gitlab.get_effective_instance_url())
    for base in candidates:
        parsed = urlparse(base if "://" in base else f"https://{base}")
        if parsed.netloc.lower() == loc.host:
            return f"{parsed.scheme}://{parsed.netloc}"
    raise MarketplaceFetchError(
        f"{loc.host} is not a GitLab instance connected to jeanclode — "
        "only GitHub and the organization's own GitLab can host skills"
    )


def _token_for(loc: RepoLocator, auth: RepoAuth | None, instance: str | None) -> str | None:
    """The org token, only when ``loc`` lives on the org's own host."""
    if not auth or not auth.token or auth.provider != loc.provider:
        return None
    if loc.provider == "github":
        return auth.token
    org_host = urlparse(auth.base_url or "").netloc.lower()
    return auth.token if instance and org_host and org_host == loc.host else None


async def fetch_repo_file(
    app: Application, loc: RepoLocator, file_path: str, auth: RepoAuth | None
) -> tuple[int, str]:
    """``(status, text)`` of one file at ``loc.ref`` (default branch when unset)."""
    if loc.provider == "github":
        if not app.github:
            raise MarketplaceFetchError("GitHub plugin is not enabled")
        owner, repo = loc.project_path.split("/", 1)
        return await app.github.fetch_repo_file_text(
            owner, repo, file_path, ref=loc.ref, auth_token=_token_for(loc, auth, None)
        )
    if not app.gitlab:
        raise MarketplaceFetchError("GitLab plugin is not enabled")
    instance = _gitlab_instance(app, loc, auth)
    return await app.gitlab.fetch_repo_file_text(
        loc.project_path,
        file_path,
        ref=loc.ref,
        auth_token=_token_for(loc, auth, instance),
        provider_url=instance,
    )


async def list_repo_files(
    app: Application, loc: RepoLocator, auth: RepoAuth | None
) -> tuple[int, list[str], bool]:
    """``(status, blob paths, truncated)`` under ``loc.subpath`` at ``loc.ref``."""
    if loc.provider == "github":
        if not app.github:
            raise MarketplaceFetchError("GitHub plugin is not enabled")
        owner, repo = loc.project_path.split("/", 1)
        return await app.github.list_repo_blob_paths(
            owner, repo, ref=loc.ref, auth_token=_token_for(loc, auth, None)
        )
    if not app.gitlab:
        raise MarketplaceFetchError("GitLab plugin is not enabled")
    instance = _gitlab_instance(app, loc, auth)
    return await app.gitlab.list_repo_blob_paths(
        loc.project_path,
        ref=loc.ref,
        path=loc.subpath,
        auth_token=_token_for(loc, auth, instance),
        provider_url=instance,
        max_pages=MAX_TREE_PAGES,
    )


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------


def find_skill_dirs(paths: list[str], subpath: str | None = None) -> list[str]:
    """Folders holding a ``SKILL.md``, relative to the repo root (``"."`` = root).

    When the source folder itself is a skill, it is the only one: that's what
    pointing at a single skill means. Skills don't nest, so a ``SKILL.md``
    under another skill's folder is part of that skill, not a new one.
    """
    prefix = f"{subpath}/" if subpath else ""
    dirs: set[str] = set()
    for path in paths:
        if not path.startswith(prefix) or posixpath.basename(path) != SKILL_FILE:
            continue
        parent = posixpath.dirname(path)
        if _IGNORED_DIRS.intersection(parent.split("/")):
            continue
        dirs.add(parent or ".")

    own = subpath or "."
    if own in dirs:
        return [own]

    top_level: list[str] = []
    for d in sorted(dirs, key=lambda d: (d.count("/"), d)):
        if not any(d.startswith(f"{outer}/") for outer in top_level):
            top_level.append(d)
    return sorted(top_level)


def skill_names(skill_dirs: list[str], repo_name: str) -> dict[str, str]:
    """Install name per skill folder: its folder name, or its path where two share one.

    Folder-based so a skill's frontmatter rename doesn't orphan its installs.
    """
    base = {d: (repo_name if d == "." else posixpath.basename(d)) for d in skill_dirs}
    counts: dict[str, int] = {}
    for name in base.values():
        counts[name] = counts.get(name, 0) + 1
    return {d: (name if counts[name] == 1 else d) for d, name in base.items()}


_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*(\n|$)", re.DOTALL)


def skill_description(text: str) -> str | None:
    """``description`` from a SKILL.md's frontmatter, trimmed for display."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    lines = match.group("body").splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value in ("", "|", ">", "|-", ">-"):
            continue_lines = []
            for nxt in lines[i + 1 :]:
                if nxt and not nxt[0].isspace():
                    break
                continue_lines.append(nxt.strip())
            value = " ".join(x for x in continue_lines if x)
        value = value.strip().strip("'\"")
        if not value:
            return None
        if len(value) > _DESCRIPTION_MAX_CHARS:
            value = value[: _DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
        return value
    return None


async def _describe(
    app: Application, loc: RepoLocator, skill_dirs: list[str], auth: RepoAuth | None
) -> dict[str, str]:
    semaphore = asyncio.Semaphore(_DESCRIBE_CONCURRENCY)

    async def one(d: str) -> tuple[str, str | None]:
        path = SKILL_FILE if d == "." else f"{d}/{SKILL_FILE}"
        async with semaphore:
            try:
                status, text = await fetch_repo_file(app, loc, path, auth)
            except Exception:
                logger.warning("could not read %s in %s", path, loc.canonical_url, exc_info=True)
                return d, None
        return d, skill_description(text) if status == 200 else None

    results = await asyncio.gather(*(one(d) for d in skill_dirs[:MAX_DESCRIBED_SKILLS]))
    return {d: desc for d, desc in results if desc}


async def discover_skills_manifest(
    app: Application, loc: RepoLocator, auth: RepoAuth | None, *, describe: bool
) -> MarketplaceManifest:
    """A manifest with one plugin per ``SKILL.md`` folder in the repo."""
    status, paths, truncated = await list_repo_files(app, loc, auth)
    if status in (401, 403, 404):
        raise MarketplaceFetchError(
            f"can't read {loc.canonical_url} (HTTP {status}) — check the repository, ref "
            "and folder exist and that the organization's token can read it"
        )
    if status >= 400:
        raise MarketplaceFetchError(f"listing {loc.canonical_url} failed: HTTP {status}")

    skill_dirs = find_skill_dirs(paths, loc.subpath)
    if not skill_dirs:
        hint = " (the file listing was cut short — link the folder that holds the skills)"
        raise MarketplaceFetchError(
            f"no {MARKETPLACE_FILE} and no {SKILL_FILE} found in {loc.canonical_url}"
            + (hint if truncated else "")
        )
    if truncated:
        logger.warning("file listing of %s was truncated; skills may be missing", loc.canonical_url)
    if len(skill_dirs) > MAX_SKILLS:
        logger.warning(
            "%s has %d skills, keeping the first %d", loc.canonical_url, len(skill_dirs), MAX_SKILLS
        )
        skill_dirs = skill_dirs[:MAX_SKILLS]

    descriptions = await _describe(app, loc, skill_dirs, auth) if describe else {}
    names = skill_names(skill_dirs, loc.name)
    title = loc.project_path + (f"/{loc.subpath}" if loc.subpath else "")
    return MarketplaceManifest(
        name=title,
        kind="skills",
        plugins=[
            MarketplacePlugin(
                name=names[d],
                description=descriptions.get(d),
                source="./",
                skills=["." if d == "." else f"./{d}"],
            )
            for d in skill_dirs
        ],
    )


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def parse_marketplace_response(status: int, text: str, git_url: str) -> MarketplaceManifest:
    """Parse a raw HTTP response into a ``MarketplaceManifest``."""
    if status == 404:
        raise MarketplaceFetchError(f"{MARKETPLACE_FILE} not found at {git_url}")
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


async def load_source_manifest(
    git_url: str, *, auth: RepoAuth | None = None, describe: bool = False
) -> MarketplaceManifest:
    """The plugins a source offers: its marketplace.json, else its SKILL.md folders.

    A source narrowed to a folder is always scanned for skills — a marketplace
    lives at its repo's root. ``describe`` reads each skill's description,
    which the dashboard shows and dispatch doesn't need.
    """
    app = get_current_app()
    loc = parse_repo_locator(git_url)
    if loc.subpath is None:
        status, text = await fetch_repo_file(app, loc, MARKETPLACE_FILE, auth)
        if status != 404:
            return parse_marketplace_response(status, text, git_url)
    if not describe:
        return await discover_skills_manifest(app, loc, auth, describe=False)

    # The dashboard reloads this on every view, and describing costs one
    # request per skill — unauthenticated GitHub allows 60 an hour.
    token = (auth.token if auth else None) or ""
    key = (loc.canonical_url, hashlib.sha256(token.encode()).hexdigest())
    cached = _described_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    manifest = await discover_skills_manifest(app, loc, auth, describe=True)
    if len(_described_cache) >= _DESCRIBED_CACHE_MAX:
        _described_cache.clear()
    _described_cache[key] = (time.monotonic() + DESCRIBED_CACHE_TTL_SECONDS, manifest)
    return manifest
