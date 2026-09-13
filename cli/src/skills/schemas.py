"""Pydantic schemas for the skills layer."""

from pathlib import Path

from pydantic import BaseModel


class Skill(BaseModel):
    """A user-loaded skill — one ``skills/<name>/SKILL.md`` inside a
    Claude Code plugin folder.

    The agent invokes it at runtime via the SDK's ``Skill`` tool. The
    plugin folder (``skill_dir.parent.parent``) is what gets passed to
    ``ClaudeAgentOptions.plugins`` so the SDK can register the skill.
    """

    name: str
    description: str
    skill_dir: Path

    @property
    def plugin_path(self) -> Path:
        return self.skill_dir.parent.parent
