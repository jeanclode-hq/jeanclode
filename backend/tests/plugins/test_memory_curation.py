"""Memory curator dispatch: which workspaces are claimed, and what a launch builds."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func

from api.database import db_create_workspace
from api.database.execution import db_create_execution
from api.database.llm_credentials import LLMCredentialAvailability
from api.database.memory import (
    db_claim_workspaces_due_for_memory_curation,
    db_create_memory_entry,
    db_delete_memory_entries,
    db_mark_memory_entries_curated,
)
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.workspaces import Workspace
from api.plugins.container.dispatch_inputs import DispatchInputs, LLMSelectionResult
from api.plugins.memory.consumer import ExecutionStatusMessage, handle_execution_status
from api.plugins.memory.launch import launch_memory_curation

DAY = timedelta(hours=24)


def _workspace(db_session) -> uuid.UUID:
    return db_create_workspace(
        db=db_session, name="curation", slug=f"curation-{uuid.uuid4().hex[:8]}"
    ).id


def _entry(db_session, workspace_id: uuid.UUID, path: str = "repo/a.md") -> None:
    db_create_memory_entry(db_session, workspace_id, path, "content", name=path)


def _claim(db_session) -> list[uuid.UUID]:
    return db_claim_workspaces_due_for_memory_curation(db_session, cadence=DAY, limit=50)


def test_claims_workspace_with_due_entries_once_per_cadence(db_session):
    workspace_id = _workspace(db_session)
    _entry(db_session, workspace_id)

    assert workspace_id in _claim(db_session)
    assert workspace_id not in _claim(db_session)


def test_claims_again_once_cadence_elapsed(db_session):
    workspace_id = _workspace(db_session)
    _entry(db_session, workspace_id)
    _claim(db_session)
    db_session.query(Workspace).filter(Workspace.id == workspace_id).update(
        {Workspace.memory_curated_at: func.now() - DAY - timedelta(minutes=1)},
        synchronize_session=False,
    )
    db_session.commit()

    assert workspace_id in _claim(db_session)


def test_skips_workspace_with_nothing_due(db_session):
    curated = _workspace(db_session)
    _entry(db_session, curated)
    db_mark_memory_entries_curated(db_session, curated, ["repo/a.md"])
    deleted = _workspace(db_session)
    _entry(db_session, deleted)
    db_delete_memory_entries(db_session, deleted, "repo/a.md")
    empty = _workspace(db_session)

    claimed = _claim(db_session)
    assert not {curated, deleted, empty} & set(claimed)


def _app(db_session, *, watcher: bool = True) -> MagicMock:
    app = MagicMock()

    async def run_in_session(fn):
        return fn(db_session)

    app.database.run_in_session.side_effect = run_in_session
    app.container = None
    if watcher:
        app.memory.watcher.backend.start_container = AsyncMock(return_value="container-1")
    else:
        app.memory.watcher = None
    return app


def _llm(available: bool = True):
    def add_llm(inputs: DispatchInputs, **_: object) -> LLMSelectionResult:
        if not available:
            return LLMSelectionResult(availability=LLMCredentialAvailability.NONE_CONFIGURED)
        inputs.public_env["JEANCLODE_MODEL"] = "m"
        return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)

    return add_llm


def _curation_executions(db_session) -> list[Execution]:
    return (
        db_session.query(Execution)
        .filter(Execution.workflow == ExecutionWorkflow.MEMORY_CURATE.value)
        .all()
    )


@pytest.mark.asyncio
async def test_launch_runs_memory_curate_with_llm_and_memory_only(db_session):
    workspace_id = _workspace(db_session)
    app = _app(db_session)

    with (
        patch("api.plugins.memory.launch.get_current_app", return_value=app),
        patch("api.plugins.memory.launch.add_llm_to_inputs", _llm()),
        patch("api.plugins.memory.launch.add_memory_to_inputs", AsyncMock()) as add_memory,
    ):
        container_id = await launch_memory_curation(workspace_id)

    assert container_id == "container-1"
    request = app.memory.watcher.backend.start_container.call_args.kwargs["request"]
    assert request.command == ["memory-curate", str(workspace_id)]
    assert not any("GITHUB" in k or "GITLAB" in k for k in request.secrets)
    [execution] = [e for e in _curation_executions(db_session) if e.container_id == "container-1"]
    assert execution.provider == "memory"
    assert execution.issues == [] and execution.pull_requests == []
    assert add_memory.call_args.kwargs == {
        "workspace_id": workspace_id,
        "execution_id": execution.id,
    }


@pytest.mark.asyncio
async def test_launch_fails_execution_when_no_llm(db_session):
    workspace_id = _workspace(db_session)
    app = _app(db_session)
    before = {e.id for e in _curation_executions(db_session)}

    with (
        patch("api.plugins.memory.launch.get_current_app", return_value=app),
        patch("api.plugins.memory.launch.add_llm_to_inputs", _llm(available=False)),
    ):
        assert await launch_memory_curation(workspace_id) is None

    [execution] = [e for e in _curation_executions(db_session) if e.id not in before]
    assert execution.status == ExecutionStatus.FAILED.value
    app.memory.watcher.backend.start_container.assert_not_called()


@pytest.mark.asyncio
async def test_launch_without_watcher_creates_nothing(db_session):
    workspace_id = _workspace(db_session)
    before = len(_curation_executions(db_session))

    with patch(
        "api.plugins.memory.launch.get_current_app", return_value=_app(db_session, watcher=False)
    ):
        assert await launch_memory_curation(workspace_id) is None

    assert len(_curation_executions(db_session)) == before


def _execution(db_session) -> Execution:
    return db_create_execution(
        db_session, provider="memory", workflow=ExecutionWorkflow.MEMORY_CURATE.value
    )


@pytest.mark.asyncio
async def test_status_consumer_records_completion(db_session):
    execution = _execution(db_session)

    with patch("api.plugins.memory.consumer.get_current_app", return_value=_app(db_session)):
        await handle_execution_status(
            ExecutionStatusMessage(execution_id=str(execution.id), status="completed")
        )

    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_status_consumer_ends_rate_limited_run_as_failed(db_session):
    execution = _execution(db_session)

    with patch("api.plugins.memory.consumer.get_current_app", return_value=_app(db_session)):
        await handle_execution_status(
            ExecutionStatusMessage(
                execution_id=str(execution.id),
                status=ExecutionStatus.SCHEDULED.value,
                retry_at="2026-09-27T00:00:00+00:00",
            )
        )

    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.error_type == "rate_limited"
    assert execution.retry_at is None
