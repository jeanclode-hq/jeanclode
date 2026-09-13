"""EchoWorkflow — minimal smoke workflow that exercises the full stack.

Workflow → BaseAgent.invoke → @activity → EventBus → display subscriber.
Used by the runner test and the discovery test, and reachable manually
via `jeanclode echo "..."`.
"""

from typing import ClassVar

from pydantic import BaseModel

from src.activities.decorator import activity
from src.agents.base import BaseAgent
from src.runtime.context import RunContext
from src.workflows.base import register
from src.workflows.schemas import WorkflowResult


class EchoInput(BaseModel):
    message: str


class EchoAgent(BaseAgent):
    name: ClassVar[str] = "EchoAgent"
    prompt_file: ClassVar[str] = "echo.md"
    allowed_tools: ClassVar[list[str]] = []
    max_turns: ClassVar[int] = 1


@activity
def record_echo(text: str, *, ctx: RunContext) -> str:  # noqa: ARG001 — ctx required by @activity
    """Trivial deterministic activity — proves the @activity path is wired."""
    return text.strip()


@register
class EchoWorkflow:
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "Smoke workflow — echoes the given message back."
    triggers: ClassVar[list[str]] = ["command:echo"]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        # CLI dispatch has no adaptor for a bare "command:echo" trigger (no
        # URL to match), so the message arrives as ctx.issues[0] rather than
        # through the env-building path every URL-driven workflow uses.
        # ECHO_MESSAGE stays as a fallback for direct RunContext callers.
        message = ctx.issues[0] if ctx.issues else ctx.env.get("ECHO_MESSAGE", "hello")
        agent_input = EchoInput(message=message)
        result = await EchoAgent().invoke(agent_input, ctx)
        normalized = record_echo(result.text, ctx=ctx)
        return WorkflowResult(
            status="success",
            summary=f"echoed {len(normalized)} chars",
            data={"text": normalized},
        )
