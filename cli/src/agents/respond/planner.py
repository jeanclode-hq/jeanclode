"""PlannerAgent — autonomous handler for one @jeanclode mention.

The agent reads the inlined context and either ``route``s (defers to
an existing dedicated pipeline) or ``handle``s the ask directly via
Bash (``gh`` / ``glab`` / ``git``) — replying, resolving a thread,
editing an issue, committing and pushing a code change, whatever the
ask calls for. Python never dispatches on which one it was; the only
Python-side follow-up is a deterministic check for whether the
planner pushed new commits (see ``workflows.jeanclode_respond``).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.agents.respond.base import RespondAgent
from src.agents.respond.schemas import PlannerOutput


class PlannerAgent(RespondAgent):
    name: ClassVar[str] = "respond-planner"
    prompt_file: ClassVar[str] = "planner.md"
    allowed_tools: ClassVar[list[str]] = ["Read", "Bash", "Edit", "Write", "Grep", "Glob"]
    max_turns: ClassVar[int] = 150
    output_schema: ClassVar[type[BaseModel] | None] = PlannerOutput
    use_third_party_skills: ClassVar[bool] = True
    use_mcp_connectors: ClassVar[bool] = True
    use_memory: ClassVar[bool] = True
    use_continuity: ClassVar[bool] = True
