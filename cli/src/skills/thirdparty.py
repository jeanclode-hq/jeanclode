"""Materialize the third-party marketplace plugins the backend resolved.

The backend resolves an org's installed marketplace plugins into clone-ready
specs and hands them to the container as the ``JEANCLODE_THIRDPARTY_PLUGINS``
env var (see
``backend/api/plugins/container/utils.py:resolve_third_party_plugins_env_for_org``).
That module's docstring says "the CLI reads ``JEANCLODE_THIRDPARTY_PLUGINS``
and clones each spec at runtime" — this module is that other half of the
contract. ``src.skills.discovery`` only ever reads ``JEANCLODE_SKILLS``
(colon-separated *local* plugin folders), so without this step a
marketplace-installed skill is resolved on the backend, allowlisted on the
proxy, but never actually reaches the agent: ``ctx.skills`` stays empty and
the whole third-party-skill mechanism in ``agents/base.py`` never activates.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from src.workspace import clone_repo

logger = logging.getLogger(__name__)

THIRDPARTY_PLUGINS_ENV_VAR = "JEANCLODE_THIRDPARTY_PLUGINS"
THIRDPARTY_PLUGINS_ENABLED_ENV_VAR = "JEANCLODE_THIRDPARTY_PLUGINS_ENABLED"
SKILL_FILE = "SKILL.md"


def clone_thirdparty_plugins_from_env(
    workspace: Path, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Clone every plugin spec in ``JEANCLODE_THIRDPARTY_PLUGINS``.

    Returns the resolved local plugin root for each spec, ready to hand to
    ``src.skills.discovery.load_skills``. Each spec is cloned into its own
    subdirectory of ``workspace`` — never shared — so two installs pinned
    to different refs of the same marketplace repo can't collide.

    A resolved root is the plugin folder itself when it has a ``plugin.json``
    and nothing else to add, otherwise a shim (see
    ``_materialize_skills_shim``) holding the skills the spec's ``skills``
    list and the ``plugin.json``'s own ``skills`` field name, plus its
    ``skills/`` folder. ``skills`` paths are relative to the plugin root, as
    in Claude Code; a root ``SKILL.md`` repo arrives as ``["."]``.

    A spec that fails in any way (clone, bad ref or sha, missing path,
    nothing to load) is logged and skipped rather than failing the whole run
    — one broken source shouldn't take down every other skill.
    """
    env = os.environ if env is None else env
    if env.get(THIRDPARTY_PLUGINS_ENABLED_ENV_VAR) != "1":
        return []
    raw = env.get(THIRDPARTY_PLUGINS_ENV_VAR, "").strip()
    if not raw:
        return []

    try:
        specs = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("could not parse %s as JSON", THIRDPARTY_PLUGINS_ENV_VAR)
        return []
    if not isinstance(specs, list):
        logger.warning("%s is not a JSON array", THIRDPARTY_PLUGINS_ENV_VAR)
        return []

    roots: list[Path] = []
    for idx, spec in enumerate(specs):
        if not isinstance(spec, dict) or not spec.get("git_url"):
            logger.warning("ignoring malformed third-party plugin spec #%d: %r", idx, spec)
            continue
        display_name = spec.get("display_name") or spec["git_url"]
        try:
            root = _materialize_spec(workspace / ".thirdparty-plugins", idx, spec, display_name)
        except Exception:
            logger.warning(
                "failed to load third-party plugin %s (%s)",
                display_name,
                spec["git_url"],
                exc_info=True,
            )
            continue
        if root is not None:
            roots.append(root)

    return roots


def _materialize_spec(base: Path, idx: int, spec: dict, display_name: str) -> Path | None:
    """Clone one spec and return a plugin folder ``load_skills`` can read, or ``None``."""
    ref = spec.get("ref")
    sha = spec.get("sha")
    subpath = spec.get("plugin_subpath")

    # A sha outlives the branch or tag it was cut from, so clone the default
    # branch and fetch the commit rather than trust ``ref``.
    clone_root = clone_repo(spec["git_url"], base / str(idx), ref=None if sha else ref)
    if sha:
        _checkout_sha(clone_root, sha)

    plugin_root = (clone_root / subpath) if subpath else clone_root
    if not _is_within(plugin_root, clone_root) or not plugin_root.is_dir():
        logger.warning(
            "third-party plugin %s path %r is missing or leaves its repo — skipping",
            display_name,
            subpath,
        )
        return None

    manifest = _read_plugin_json(plugin_root)
    extra_paths = _as_paths(spec.get("skills")) + _as_paths((manifest or {}).get("skills"))
    if manifest is not None and not extra_paths:
        return plugin_root
    if manifest is None and not extra_paths:
        # A plugin repo without plugin.json still has the default skills/ folder.
        extra_paths = ["skills"]

    skill_dirs = _collect_skill_dirs(plugin_root, clone_root, extra_paths, display_name)
    if manifest is not None:
        if not skill_dirs:
            return plugin_root
        # Claude Code adds listed skills to the plugin's own, it doesn't replace them.
        skill_dirs = _skills_under(plugin_root / "skills", clone_root) + skill_dirs
    if not skill_dirs:
        logger.warning(
            "third-party plugin %s has no plugin.json and none of its listed skills "
            "exist at %s — skipping",
            display_name,
            plugin_root,
        )
        return None
    return _materialize_skills_shim(base / f"{idx}-shim", skill_dirs, clone_root, display_name)


def _read_plugin_json(plugin_root: Path) -> dict | None:
    path = plugin_root / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        logger.warning("unreadable plugin.json at %s, ignoring its contents", path)
        return {}
    return data if isinstance(data, dict) else {}


def _as_paths(value: object) -> list[str]:
    """Claude Code accepts one path or a list of them for ``skills``."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _checkout_sha(clone_root: Path, sha: str) -> None:
    for cmd in (
        ["git", "-C", str(clone_root), "fetch", "--quiet", "--depth", "1", "origin", sha],
        ["git", "-C", str(clone_root), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ):
        subprocess.run(cmd, check=True, capture_output=True, text=True)


def _is_within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _skills_under(folder: Path, clone_root: Path) -> list[Path]:
    """Skill folders directly inside ``folder``, the way a ``skills/`` dir holds them."""
    if not folder.is_dir():
        return []
    return [
        child
        for child in sorted(folder.iterdir())
        if child.is_dir() and _is_skill(child, clone_root)
    ]


def _is_skill(folder: Path, clone_root: Path) -> bool:
    md = folder / SKILL_FILE
    return md.is_file() and _is_within(md, clone_root)


def _collect_skill_dirs(
    plugin_root: Path, clone_root: Path, rel_paths: list[str], display_name: str
) -> list[Path]:
    """Resolve each listed path to skill folders.

    A path is either one skill (it holds a ``SKILL.md``) or a folder of them,
    like ``skills/`` — Claude Code's entries use both. Anything resolving
    outside the cloned repo, through ``..`` or a symlink, is refused: the
    rest of the container's filesystem is not the marketplace's to expose.
    """
    found: list[Path] = []
    for rel in rel_paths:
        src = plugin_root / rel
        if not _is_within(src, clone_root):
            logger.warning(
                "third-party plugin %s skill path %r leaves its repo — skipping", display_name, rel
            )
            continue
        if _is_skill(src, clone_root):
            found.append(src)
            continue
        nested = _skills_under(src, clone_root)
        if not nested:
            logger.warning(
                "third-party plugin %s lists skill %r with no SKILL.md at %s — skipping",
                display_name,
                rel,
                src,
            )
        found += nested
    return found


def _materialize_skills_shim(
    shim_root: Path, skill_dirs: list[Path], clone_root: Path, display_name: str
) -> Path | None:
    """Build a synthetic plugin folder holding exactly ``skill_dirs``.

    For a marketplace entry scoped by a ``skills`` list rather than its own
    ``plugin.json`` (several plugins sharing one repo, a plain repo of
    ``SKILL.md`` folders), or a plugin whose ``plugin.json`` lists skills
    outside ``skills/``. Each folder is symlinked into ``<shim_root>/skills/``
    so the result satisfies ``discovery.load_skills``'s plugin-folder
    contract without copying files.
    """
    skills_dir = shim_root / "skills"
    seen: set[Path] = set()
    for src in skill_dirs:
        resolved = src.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        skills_dir.mkdir(parents=True, exist_ok=True)
        # A repo that is one skill clones into a folder named after the URL.
        wanted = display_name if resolved == clone_root.resolve() else resolved.name
        (skills_dir / _link_name(skills_dir, wanted)).symlink_to(resolved, target_is_directory=True)

    if not seen:
        return None
    plugin_json_dir = shim_root / ".claude-plugin"
    plugin_json_dir.mkdir(parents=True, exist_ok=True)
    (plugin_json_dir / "plugin.json").write_text(json.dumps({"name": display_name}))
    return shim_root


def _link_name(skills_dir: Path, wanted: str) -> str:
    """``wanted``, or ``wanted-2``… when two skill folders share a name."""
    name = re.sub(r"[^\w.-]", "-", wanted).strip(".") or "skill"
    candidate, n = name, 2
    while (skills_dir / candidate).exists() or (skills_dir / candidate).is_symlink():
        candidate, n = f"{name}-{n}", n + 1
    return candidate
