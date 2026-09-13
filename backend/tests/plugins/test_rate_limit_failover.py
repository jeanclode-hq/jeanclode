"""Regression tests for ADR-010 retry_at selection on a 429 (backend.py).

A rate-limited credential must not park the execution on *its own*
stale_until when another credential in the pool is already usable —
retry_at should reflect the pool's real state, not the failing row alone.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from api.models.executions import Execution, ExecutionWorkflow
from api.plugins.container.docker import DockerBackend
from api.services.llm_credentials import create_llm_credential


def _make_backend() -> DockerBackend:
    from api.plugins.container.config import DockerConfig

    return DockerBackend("test", DockerConfig(), AsyncMock(), AsyncMock())


def _make_execution(db_session, provider="github", workflow=ExecutionWorkflow.RESPOND) -> Execution:
    execution = Execution(provider=provider, workflow=workflow.value)
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    return execution


async def test_retries_immediately_when_another_credential_is_available(app, db_session):
    exhausted = create_llm_credential(
        db_session,
        kind="oauth_subscription",
        provider="claude_code",
        secret="oauth-token",
    )
    create_llm_credential(
        db_session,
        kind="api_key",
        provider="anthropic",
        secret="sk-ant-fallback",
    )
    execution = _make_execution(db_session)

    backend = _make_backend()
    retry_at = await backend._handle_rate_limit(
        str(execution.id),
        [{"credential_id": str(exhausted.id), "retry_after": None}],
    )

    assert retry_at is not None
    assert retry_at <= datetime.now(UTC) + timedelta(seconds=1)


async def test_waits_for_pool_when_every_credential_is_stale(app, db_session):
    only = create_llm_credential(
        db_session,
        kind="oauth_subscription",
        provider="claude_code",
        secret="oauth-token",
    )
    execution = _make_execution(db_session)

    backend = _make_backend()
    retry_at = await backend._handle_rate_limit(
        str(execution.id),
        [{"credential_id": str(only.id), "retry_after": 120}],
    )

    assert retry_at is not None
    assert retry_at > datetime.now(UTC) + timedelta(minutes=1)
