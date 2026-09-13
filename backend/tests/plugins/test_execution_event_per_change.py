"""A status change publishes one SSE event, however many issues/PRs the run links."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _db_plugin() -> MagicMock:
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    db_plugin = MagicMock()
    db_plugin.session.return_value = ctx
    return db_plugin


@pytest.mark.asyncio
async def test_sentry_batch_status_change_publishes_one_event():
    from api.plugins.sentry import consumer as consumer_mod
    from api.plugins.sentry.consumer import ExecutionStatusMessage, _handle_execution_status

    issue_ids = [uuid.uuid4() for _ in range(3)]
    fake_exec = MagicMock(status="running", error_type=None, error_detail=None)
    app = MagicMock()
    app.database = _db_plugin()
    publish = AsyncMock()

    with (
        patch.object(consumer_mod, "get_current_app", return_value=app),
        patch.object(consumer_mod, "db_update_execution_status", return_value=fake_exec),
        patch.object(consumer_mod, "_resolve_execution_targets", return_value=("ws-1", issue_ids)),
        patch.object(consumer_mod, "publish_execution_event", new=publish),
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(uuid.uuid4()), status="running")
        )

    publish.assert_awaited_once()
    payload = publish.await_args.kwargs["payload"]
    assert payload["issue_ids"] == [str(i) for i in issue_ids]
    assert payload["issue_id"] == str(issue_ids[0])
    assert payload["status"] == "running"


@pytest.mark.asyncio
async def test_sentry_status_change_without_issues_still_publishes():
    from api.plugins.sentry import consumer as consumer_mod
    from api.plugins.sentry.consumer import ExecutionStatusMessage, _handle_execution_status

    fake_exec = MagicMock(status="failed", error_type="boom", error_detail=None)
    app = MagicMock()
    app.database = _db_plugin()
    publish = AsyncMock()

    with (
        patch.object(consumer_mod, "get_current_app", return_value=app),
        patch.object(consumer_mod, "db_update_execution_status", return_value=fake_exec),
        patch.object(consumer_mod, "_resolve_execution_targets", return_value=("", [])),
        patch.object(consumer_mod, "publish_execution_event", new=publish),
    ):
        await _handle_execution_status(
            ExecutionStatusMessage(execution_id=str(uuid.uuid4()), status="failed")
        )

    publish.assert_awaited_once()
    payload = publish.await_args.kwargs["payload"]
    assert payload["issue_id"] is None
    assert payload["issue_ids"] == []
