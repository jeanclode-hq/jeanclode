"""Load full skill instructions for agents that must follow a skill by contract."""

from __future__ import annotations

import logging

from src.agents.utils import strip_frontmatter
from src.skills.schemas import Skill

logger = logging.getLogger(__name__)


def read_skill_instructions(skill: Skill) -> str:
    """Return SKILL.md body (frontmatter stripped) for inlining into a prompt."""
    md = skill.skill_dir / "SKILL.md"
    try:
        text = md.read_text()
    except OSError:
        logger.warning("could not read skill instructions: %s", md, exc_info=True)
        return ""
    return strip_frontmatter(text).strip()


def find_skill_by_name(skills: list[Skill], name: str) -> Skill | None:
    """Match by frontmatter ``name`` or directory name."""
    target = name.strip()
    if not target:
        return None
    for skill in skills:
        if skill.name == target or skill.skill_dir.name == target:
            return skill
    return None
