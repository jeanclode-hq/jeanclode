"""MemoryCurateWorkflow — periodic pruning of a workspace's agent memory.

Dispatched by the backend's curation poller with the workspace id as its
only argument (the memory token already scopes every call to it). Entries
changed since the last pass are curated in batches, each by a fresh
curator session so one context never holds the whole store; a batch is
marked curated only once its session finishes, so a failed run leaves its
entries due for the next one.
"""

from typing import ClassVar

from src.activities.memory import fetch_paths_due_for_curation, mark_curated
from src.agents.memory import MemoryCuratorAgent, MemoryCuratorInput
from src.runtime.context import RunContext
from src.workflows.base import register
from src.workflows.schemas import WorkflowResult

BATCH_SIZE = 20
# Keeps one run inside the container timeout; whatever's left stays due for the next run.
MAX_BATCHES_PER_RUN = 5


@register
class MemoryCurateWorkflow:
    name: ClassVar[str] = "memory-curate"
    description: ClassVar[str] = "Prune, merge and rewrite a workspace's agent memory."
    triggers: ClassVar[list[str]] = ["command:memory-curate"]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        if not ctx.memory_enabled:
            return WorkflowResult(status="error", summary="memory is not configured for this run")

        paths = await fetch_paths_due_for_curation(ctx=ctx)
        batches = [paths[i : i + BATCH_SIZE] for i in range(0, len(paths), BATCH_SIZE)]
        batches = batches[:MAX_BATCHES_PER_RUN]

        curated = 0
        outcomes: list[str] = []
        for batch in batches:
            result = await MemoryCuratorAgent().invoke(MemoryCuratorInput(paths=batch), ctx)
            curated += await mark_curated(batch, ctx=ctx)
            outcomes.append(result.text.strip())

        return WorkflowResult(
            status="success",
            summary=f"curated {sum(map(len, batches))} of {len(paths)} due entries",
            data={"due": len(paths), "marked": curated, "outcomes": outcomes},
        )
