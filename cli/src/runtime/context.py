"""RunContext — the per-run state object passed to every activity and agent.

Carries cwd, env, workspace, model, and the event bus. `with_cwd` returns a
derived context, which is how per-group worktree isolation is expressed
(no more "ask the model to please cd into the worktree first").
"""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.bus import EventBus
from src.runtime.events import Event
from src.runtime.llm_options import LLMOption
from src.runtime.mcp_connectors import McpServerSpec
from src.skills.schemas import Skill


class RunContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)
    workspace: Path
    model: str | None = None
    small_model: str | None = None
    events: EventBus
    skills: list[Skill] = Field(default_factory=list)
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    # Repos cloned alongside the primary one (``--related-repo``), as
    # ``{"name", "path"}`` dicts — repos grouped with the primary via the
    # backend's RepositoryMapping, cloned into the same workspace so the
    # agent can read across the group. See ``BaseAgent.invoke``, which
    # injects this into the prompt.
    related_repos: list[dict[str, str]] = Field(default_factory=list)
    # Provider handles to @-mention once a bot-opened PR/MR is ready, from
    # JEANCLODE_NOTIFY_USERS. Empty is the off switch — see src.runtime.notify.
    notify_users: list[str] = Field(default_factory=list)
    # From JEANCLODE_LLM_OPTIONS; empty unless triage has a fixer LLM to pick.
    llm_options: list[LLMOption] = Field(default_factory=list)
    dry_run: bool = False
    debug: bool = False
    memory_enabled: bool = False

    def with_cwd(self, cwd: Path) -> Self:
        """Return a context derived from this one but with a different cwd.

        Env, model, and the event bus are shared by reference — emissions
        from the derived context still reach the same subscribers.
        """
        return self.model_copy(update={"cwd": cwd})

    def emit(self, event: Event) -> None:
        self.events.publish(event)
