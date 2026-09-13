"""Discover user-loaded skills on disk.

A skill is one ``skills/<name>/SKILL.md`` inside a Claude Code plugin
folder (a folder with ``.claude-plugin/plugin.json``). The user lists
plugin folders in the ``JEANCLODE_SKILLS`` env var, colon-separated.
"""

import logging
import os
import re
from pathlib import Path

from src.skills.schemas import Skill

logger = logging.getLogger(__name__)

SKILLS_ENV_VAR = "JEANCLODE_SKILLS"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^(?P<key>[a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(?P<val>.*?)\s*$")


def load_skills_from_env(env: dict[str, str] | None = None) -> list[Skill]:
    """Read ``JEANCLODE_SKILLS`` (colon-separated plugin folders)."""
    raw = (env or os.environ).get(SKILLS_ENV_VAR, "").strip()
    if not raw:
        return []
    paths = [Path(p).expanduser() for p in raw.split(":") if p]
    return load_skills(paths)


def load_skills(plugin_paths: list[Path]) -> list[Skill]:
    """Walk each plugin folder and yield every skill it advertises.

    A path that is not a Claude Code plugin (no
    ``.claude-plugin/plugin.json``) is skipped with a warning.
    """
    skills: list[Skill] = []
    for plugin in plugin_paths:
        if not _is_plugin(plugin):
            logger.warning("skill path is not a Claude Code plugin: %s", plugin)
            continue
        skills_root = plugin / "skills"
        if not skills_root.is_dir():
            logger.warning("plugin has no skills/ directory: %s", plugin)
            continue
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                continue
            skill = _parse_skill(md, skill_dir)
            if skill is not None:
                skills.append(skill)
    return skills


def _is_plugin(path: Path) -> bool:
    return (path / ".claude-plugin" / "plugin.json").is_file()


def _parse_skill(md: Path, skill_dir: Path) -> Skill | None:
    try:
        text = md.read_text()
    except OSError:
        logger.warning("could not read SKILL.md: %s", md, exc_info=True)
        return None

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        logger.warning("SKILL.md missing frontmatter: %s", md)
        return None

    fields = _parse_frontmatter(match.group("body"))
    name = fields.get("name") or skill_dir.name
    description = fields.get("description", "").strip()
    if not description:
        logger.warning("SKILL.md missing 'description': %s", md)
        return None

    return Skill(name=name, description=description, skill_dir=skill_dir)


def _parse_frontmatter(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if m is None:
            continue
        val = m.group("val").strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        out[m.group("key")] = val
    return out
