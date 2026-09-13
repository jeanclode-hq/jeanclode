"""Third-party skills — Claude Code plugins loaded by the user per-run.

A skill is one ``skills/<name>/SKILL.md`` inside a Claude Code plugin
folder. The user lists plugin paths in the ``JEANCLODE_SKILLS`` env
var (colon-separated); the runner loads them once at workflow start
and exposes them as ``ctx.skills``.

An agent that opts in (``use_third_party_skills = True``) gets:
  - the SDK's ``Skill`` tool added to allowed_tools
  - the user's plugin folders passed via ``ClaudeAgentOptions.plugins``
  - a discovery block (name + description per skill) appended to its
    prompt so the model knows what's available to invoke
  - the attribution + guardrail reminder in that same block

Each agent's own ``prompt_file`` declares which fields skills may
influence.
"""

from src.skills.discovery import load_skills, load_skills_from_env
from src.skills.prompt import discovery_block
from src.skills.schemas import Skill
from src.skills.thirdparty import clone_thirdparty_plugins_from_env

__all__ = [
    "Skill",
    "clone_thirdparty_plugins_from_env",
    "discovery_block",
    "load_skills",
    "load_skills_from_env",
]
