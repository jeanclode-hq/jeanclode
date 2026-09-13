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
from collections.abc import Mapping
from pathlib import Path

from src.workspace import clone_repo

logger = logging.getLogger(__name__)

THIRDPARTY_PLUGINS_ENV_VAR = "JEANCLODE_THIRDPARTY_PLUGINS"
THIRDPARTY_PLUGINS_ENABLED_ENV_VAR = "JEANCLODE_THIRDPARTY_PLUGINS_ENABLED"


def clone_thirdparty_plugins_from_env(
    workspace: Path, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Clone every plugin spec in ``JEANCLODE_THIRDPARTY_PLUGINS``.

    Returns the resolved local plugin root for each spec, ready to hand to
    ``src.skills.discovery.load_skills``. Each spec is cloned into its own
    subdirectory of ``workspace`` — never shared — so two installs pinned
    to different refs of the same marketplace repo can't collide.

    A resolved root is either the folder containing ``.claude-plugin/
    plugin.json`` (the dedicated-plugin-folder layout), or, when a spec
    carries a ``skills`` allowlist instead, a synthetic shim folder built by
    ``_materialize_skills_shim`` — for marketplaces where several plugins
    share one source root and are told apart only by which ``skills/<name>``
    folders they list.

    A spec that fails to clone, or doesn't resolve to a real plugin folder
    either way (bad ``plugin_subpath``, marketplace repo restructured since
    install), is logged and skipped rather than failing the whole run — one
    broken marketplace entry shouldn't take down every other skill.
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
        if not isinstance(spec, dict):
            continue
        git_url = spec.get("git_url")
        if not git_url:
            continue
        ref = spec.get("ref")
        subpath = spec.get("plugin_subpath")
        display_name = spec.get("display_name") or git_url

        try:
            clone_root = clone_repo(git_url, workspace / ".thirdparty-plugins" / str(idx), ref=ref)
        except Exception:
            logger.warning(
                "failed to clone third-party plugin %s (%s)", display_name, git_url, exc_info=True
            )
            continue

        plugin_root = (clone_root / subpath) if subpath else clone_root
        if (plugin_root / ".claude-plugin" / "plugin.json").is_file():
            roots.append(plugin_root)
            continue

        shim_root = _materialize_skills_shim(
            workspace / ".thirdparty-plugins" / f"{idx}-shim",
            clone_root,
            spec.get("skills"),
            display_name,
        )
        if shim_root is not None:
            roots.append(shim_root)
            continue

        logger.warning(
            "third-party plugin %s has no .claude-plugin/plugin.json at %s — skipping",
            display_name,
            plugin_root,
        )

    return roots


def _materialize_skills_shim(
    shim_root: Path, clone_root: Path, skill_relpaths: object, display_name: str
) -> Path | None:
    """Build a synthetic plugin folder for a marketplace entry that shares its
    source with sibling plugins and is scoped only via a ``skills`` allowlist
    instead of owning its own ``.claude-plugin/plugin.json`` — e.g. one repo,
    several virtual plugins, each just a list of ``skills/<name>`` folders
    (a common marketplace.json layout that predates this bridge and that the
    plain ``.claude-plugin/plugin.json`` check above can't see).

    Symlinks each listed skill folder into a fresh ``<shim_root>/skills/`` so
    the result satisfies ``discovery.load_skills``'s plugin-folder contract
    without duplicating any files or requiring discovery.py changes.
    """
    if not isinstance(skill_relpaths, list) or not skill_relpaths:
        return None

    linked = False
    skills_dir = shim_root / "skills"
    for rel in skill_relpaths:
        if not isinstance(rel, str) or not rel.strip():
            continue
        src = clone_root / rel
        if not (src / "SKILL.md").is_file():
            logger.warning(
                "third-party plugin %s lists skill %r with no SKILL.md at %s — skipping",
                display_name,
                rel,
                src,
            )
            continue
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / src.name).symlink_to(src, target_is_directory=True)
        linked = True

    if not linked:
        return None

    plugin_json_dir = shim_root / ".claude-plugin"
    plugin_json_dir.mkdir(parents=True, exist_ok=True)
    (plugin_json_dir / "plugin.json").write_text(json.dumps({"name": display_name}))
    return shim_root
