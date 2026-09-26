"""Curation bookkeeping against the backend's /internal/memory/curation endpoints.

Called by the workflow, never exposed to the curator agent.
"""

from src.activities.decorator import activity
from src.agents._memory_backend import MemoryToolError, backend_request, error_detail
from src.runtime.context import RunContext


@activity(name="Listing memory due for curation")
async def fetch_paths_due_for_curation(*, ctx: RunContext) -> list[str]:  # noqa: ARG001 — ctx required by @activity
    status, data = await backend_request("GET", "/internal/memory/curation")
    if status != 200:
        raise MemoryToolError(error_detail(data))
    return [str(path) for path in data.get("paths") or []]


@activity(name="Marking memory curated")
async def mark_curated(paths: list[str], *, ctx: RunContext) -> int:  # noqa: ARG001 — ctx required by @activity
    status, data = await backend_request(
        "POST", "/internal/memory/curation/mark", json_body={"paths": paths}
    )
    if status != 200:
        raise MemoryToolError(error_detail(data))
    return int(data.get("marked_count") or 0)
