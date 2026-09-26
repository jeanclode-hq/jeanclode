"""Launch one memory-curator container for a workspace.

The run gets an LLM credential and the workspace's memory token — no git
credential, since the curator reads nothing but memory.
"""

import logging
from uuid import UUID

from api.context import get_current_app
from api.database.execution import (
    db_create_execution,
    db_update_execution_container,
    db_update_execution_status,
)
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.plugins.container.backend import ContainerRequest
from api.plugins.container.dispatch_inputs import (
    DispatchInputs,
    add_agent_tooling_hosts,
    add_llm_to_inputs,
    add_memory_to_inputs,
)
from api.plugins.container.utils import build_container_labels
from api.plugins.database.plugin import DatabasePlugin

logger = logging.getLogger(__name__)

PLUGIN_NAME = "memory"


def _image_and_timeout() -> tuple[str, int]:
    container_plugin = get_current_app().container
    if container_plugin:
        config = container_plugin.config
        if config.backend == "kubernetes" and config.kubernetes:
            return config.kubernetes.image, config.kubernetes.timeout
        if config.docker:
            return config.docker.image, config.docker.timeout
    return "jeanclode/cli:latest", 1800


async def _fail(
    db_plugin: DatabasePlugin, execution_id: UUID, error_type: str, error_detail: str
) -> None:
    await db_plugin.run_in_session(
        lambda db: db_update_execution_status(
            db,
            execution_id,
            ExecutionStatus.FAILED.value,
            error_type=error_type,
            error_detail=error_detail,
        )
    )


async def launch_memory_curation(workspace_id: UUID) -> str | None:
    """Create a MEMORY_CURATE execution for ``workspace_id`` and start its container."""
    app = get_current_app()
    plugin = app.memory
    db_plugin = app.database
    if not plugin or not plugin.watcher or not db_plugin:
        logger.error("Memory watcher or database not available, cannot dispatch curation")
        return None

    execution_id = await db_plugin.run_in_session(
        lambda db: (
            db_create_execution(
                db,
                provider=PLUGIN_NAME,
                workflow=ExecutionWorkflow.MEMORY_CURATE.value,
                trigger=ExecutionTrigger.AUTO.value,
            ).id
        )
    )

    inputs = DispatchInputs()
    if not add_llm_to_inputs(inputs).available:
        # No SCHEDULED retry: the entries stay due, so the next scan picks them up.
        await _fail(
            db_plugin, execution_id, "no_llm_credential_available", "No LLM credential available"
        )
        return None
    add_agent_tooling_hosts(inputs)
    await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

    image, timeout = _image_and_timeout()
    request = ContainerRequest(
        image=image,
        command=["memory-curate", str(workspace_id)],
        env=inputs.public_env,
        secrets=inputs.secrets,
        upstreams=inputs.upstreams,
        extra_hosts=inputs.extra_hosts,
        timeout_seconds=timeout,
        labels=build_container_labels(execution_id=str(execution_id), plugin_name=PLUGIN_NAME),
    )

    try:
        container_id = await plugin.watcher.backend.start_container(
            execution_id=str(execution_id), request=request
        )
    except Exception:
        logger.exception("Failed to dispatch memory curation for workspace %s", workspace_id)
        await _fail(
            db_plugin,
            execution_id,
            "dispatch_failed",
            "Failed to start the memory curation container",
        )
        return None

    await db_plugin.run_in_session(
        lambda db: db_update_execution_container(db, execution_id, container_id)
    )
    logger.info(
        "Dispatched memory curation %s for workspace %s", str(execution_id)[:8], workspace_id
    )
    return container_id
