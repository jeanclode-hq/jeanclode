"""Tests for the github plugin's launch helpers.

Focuses on the behaviour we own (command shape, watcher routing) and
relies on the shared dispatch_inputs tests for credential resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.database.llm_credentials import LLMCredentialAvailability
from api.models.executions import ExecutionWorkflow
from api.plugins.container.dispatch_inputs import LLMSelectionResult
from api.plugins.github.launch import (
    build_command,
    build_respond_command,
    launch_container,
    launch_respond_container,
)


def _make_pr() -> MagicMock:
    pr = MagicMock()
    pr.id = uuid4()
    pr.pr_url = "https://github.com/acme/app/pull/7"
    pr.repository = MagicMock()
    pr.repository.org_id = uuid4()
    pr.repository.auth_token_encrypted = None
    return pr


def test_build_command_review_is_just_pr_url():
    """REVIEW (the default for github URLs) is a one-element URL command."""
    pr = _make_pr()
    assert build_command(ExecutionWorkflow.REVIEW, pr) == [pr.pr_url]


def test_build_command_summary_prepends_summary():
    """SUMMARY uses the explicit `summary` subcommand."""
    pr = _make_pr()
    assert build_command(ExecutionWorkflow.SUMMARY, pr) == ["summary", pr.pr_url]


def test_build_command_unknown_workflow_falls_back_to_review_shape():
    """Unsupported workflows shouldn't silently substitute — fall back to URL."""
    pr = _make_pr()
    # FIX is not yet PR-driven. We don't crash; we let the CLI surface
    # the error using the bare URL form.
    assert build_command(ExecutionWorkflow.FIX, pr) == [pr.pr_url]


@pytest.mark.asyncio
async def test_launch_container_marks_failed_when_no_watcher():
    """If the plugin has no watcher, dispatch fails fast and updates the row."""
    pr = _make_pr()
    execution_id = uuid4()

    mock_app = MagicMock()
    mock_app.github = MagicMock()
    mock_app.github._watcher = None  # The condition under test
    mock_app.database = MagicMock()
    db_session = MagicMock()
    mock_app.database.session.return_value.__enter__ = MagicMock(return_value=db_session)
    mock_app.database.session.return_value.__exit__ = MagicMock(return_value=False)

    with (
        patch("api.plugins.github.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.github.launch.db_update_execution_status") as update_status,
    ):
        result = await launch_container(ExecutionWorkflow.REVIEW, pr, execution_id)

    assert result is None
    update_status.assert_called_once()
    _, kwargs = update_status.call_args[0], update_status.call_args[1]
    assert kwargs["error_type"] == "no_watcher"


def _make_watcher_app() -> MagicMock:
    mock_app = MagicMock()
    mock_app.github = MagicMock()
    mock_app.github._watcher = MagicMock()
    mock_app.database = MagicMock()
    db_session = MagicMock()
    mock_app.database.session.return_value.__enter__ = MagicMock(return_value=db_session)
    mock_app.database.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_app


@pytest.mark.asyncio
async def test_launch_container_fails_fast_when_no_llm_credentials_configured():
    """ADR-010: an empty credential pool is misconfiguration, not exhaustion
    — fail fast rather than scheduling a retry that will never resolve."""
    pr = _make_pr()
    execution_id = uuid4()
    mock_app = _make_watcher_app()

    llm_selection = LLMSelectionResult(availability=LLMCredentialAvailability.NONE_CONFIGURED)

    with (
        patch("api.plugins.github.launch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.github.launch.build_dispatch_inputs",
            new=AsyncMock(return_value=(MagicMock(), llm_selection)),
        ),
        patch("api.plugins.github.launch.db_update_execution_status") as update_status,
    ):
        result = await launch_container(ExecutionWorkflow.REVIEW, pr, execution_id)

    assert result is None
    update_status.assert_called_once()
    _, kwargs = update_status.call_args[0], update_status.call_args[1]
    assert kwargs["error_type"] == "no_llm_credentials_configured"


@pytest.mark.asyncio
async def test_launch_container_schedules_retry_when_every_credential_stale():
    """ADR-010: every credential temporarily stale → SCHEDULED with the
    computed retry_at, not FAILED."""
    pr = _make_pr()
    execution_id = uuid4()
    mock_app = _make_watcher_app()

    retry_at = datetime.now(UTC) + timedelta(hours=2)
    llm_selection = LLMSelectionResult(
        availability=LLMCredentialAvailability.ALL_STALE, retry_at=retry_at
    )

    with (
        patch("api.plugins.github.launch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.github.launch.build_dispatch_inputs",
            new=AsyncMock(return_value=(MagicMock(), llm_selection)),
        ),
        patch("api.plugins.github.launch.db_mark_execution_scheduled") as mark_scheduled,
    ):
        result = await launch_container(ExecutionWorkflow.REVIEW, pr, execution_id)

    assert result is None
    mark_scheduled.assert_called_once()
    _, kwargs = mark_scheduled.call_args[0], mark_scheduled.call_args[1]
    assert kwargs["retry_at"] == retry_at


@pytest.mark.asyncio
async def test_build_dispatch_inputs_filters_third_party_by_workflow():
    """The workflow value must be threaded into the third-party resolver
    so a plugin opted into 'review' doesn't leak into a 'summary' run."""
    from api.plugins.container.utils import ThirdPartyPluginsResolved
    from api.plugins.github.launch import build_dispatch_inputs

    pr = _make_pr()

    captured: dict[str, str] = {}

    async def fake_resolver(org_id, *, workflow):
        captured["workflow"] = workflow
        return ThirdPartyPluginsResolved()

    with (
        patch("api.plugins.github.launch.add_llm_to_inputs"),
        patch("api.plugins.github.launch.add_git_platform_to_inputs", new=AsyncMock()),
        patch(
            "api.plugins.github.launch.resolve_third_party_plugins_env_for_org",
            new=fake_resolver,
        ),
        patch("api.plugins.github.launch.add_connectors_to_inputs", new=AsyncMock()),
    ):
        await build_dispatch_inputs(ExecutionWorkflow.SUMMARY, pr)

    assert captured["workflow"] == "summary"


@pytest.mark.asyncio
async def test_launch_container_uses_workflow_specific_command():
    """``launch_container`` builds the right command and labels for SUMMARY."""
    pr = _make_pr()
    execution_id = uuid4()

    backend = MagicMock()
    backend.start_container = AsyncMock(return_value="container-1234567890")

    mock_app = MagicMock()
    mock_app.github = MagicMock()
    mock_app.github._watcher = MagicMock()
    mock_app.github._watcher.backend = backend

    container_plugin = MagicMock()
    container_plugin.config.backend = "docker"
    container_plugin.config.docker = MagicMock()
    container_plugin.config.docker.image = "img:latest"
    container_plugin.config.docker.timeout = 60
    mock_app.container = container_plugin

    inputs = MagicMock()
    inputs.public_env = {}
    inputs.secrets = {}
    inputs.upstreams = []
    inputs.extra_hosts = []

    llm_selection = LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)

    with (
        patch("api.plugins.github.launch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.github.launch.build_dispatch_inputs",
            new=AsyncMock(return_value=(inputs, llm_selection)),
        ),
        patch(
            "api.plugins.github.launch.db_update_execution_container",
        ),
        patch(
            "api.plugins.github.launch.resolve_tmp_size_limit",
            new=AsyncMock(return_value="1024Mi"),
        ),
    ):
        result = await launch_container(ExecutionWorkflow.SUMMARY, pr, execution_id)

    assert result == "container-1234567890"
    request = backend.start_container.await_args.kwargs["request"]
    assert request.command == ["summary", pr.pr_url]
    assert request.labels["jeanclode.plugin"] == "github"
    assert request.labels["jeanclode.execution_id"] == str(execution_id)


def test_build_respond_command():
    assert build_respond_command("https://github.com/o/r/pull/1") == [
        "respond",
        "https://github.com/o/r/pull/1",
    ]


@pytest.mark.asyncio
async def test_launch_respond_container_command_shape():
    """Respond is single-repo: it hands the container just the comment URL —
    no ``--related-repo`` flags and never a mention env payload."""
    pr = _make_pr()
    execution_id = uuid4()
    target_url = "https://github.com/acme/app/pull/7#discussion_r555"

    backend = MagicMock()
    backend.start_container = AsyncMock(return_value="container-respond-1234")

    mock_app = MagicMock()
    mock_app.github = MagicMock()
    mock_app.github._watcher = MagicMock()
    mock_app.github._watcher.backend = backend

    container_plugin = MagicMock()
    container_plugin.config.backend = "docker"
    container_plugin.config.docker = MagicMock()
    container_plugin.config.docker.image = "img:latest"
    container_plugin.config.docker.timeout = 60
    mock_app.container = container_plugin

    from api.plugins.container.utils import ThirdPartyPluginsResolved

    with (
        patch("api.plugins.github.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.github.launch.add_llm_to_inputs"),
        patch("api.plugins.github.launch.add_git_platform_to_inputs", new=AsyncMock()),
        patch("api.plugins.github.launch.add_agent_tooling_hosts"),
        patch(
            "api.plugins.github.launch.resolve_third_party_plugins_env_for_org",
            new=AsyncMock(return_value=ThirdPartyPluginsResolved()),
        ),
        patch("api.plugins.github.launch.add_connectors_to_inputs", new=AsyncMock()),
        patch("api.plugins.github.launch.db_update_execution_container"),
        patch(
            "api.plugins.github.launch.resolve_tmp_size_limit",
            new=AsyncMock(return_value="1024Mi"),
        ),
    ):
        result = await launch_respond_container(
            target_url=target_url,
            org_id=pr.repository.org_id,
            repo=pr.repository,
            execution_id=execution_id,
        )

    assert result == "container-respond-1234"
    request = backend.start_container.await_args.kwargs["request"]
    assert request.command == ["respond", target_url]
    assert request.labels["jeanclode.plugin"] == "github"
    # No mention payload should leak into env — the CLI re-fetches from API.
    for key in request.env:
        assert not key.startswith("JEANCLODE_RESPOND_"), f"unexpected env: {key}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (ExecutionWorkflow.REVIEW, True),
        (ExecutionWorkflow.RESPOND, True),
        (ExecutionWorkflow.SUMMARY, False),
    ],
)
async def test_build_dispatch_inputs_wires_notify_only_for_loop_ending_workflows(
    workflow, expected
):
    """Only REVIEW and RESPOND can emit the ready notice — they are the two
    ends of the review loop. A summary run has no business carrying the
    tenant's notify list into its container."""
    from api.plugins.container.utils import ThirdPartyPluginsResolved
    from api.plugins.github.launch import build_dispatch_inputs

    pr = _make_pr()

    async def fake_resolver(org_id, *, workflow):
        return ThirdPartyPluginsResolved()

    with (
        patch("api.plugins.github.launch.add_llm_to_inputs"),
        patch("api.plugins.github.launch.add_git_platform_to_inputs", new=AsyncMock()),
        patch(
            "api.plugins.github.launch.resolve_third_party_plugins_env_for_org",
            new=fake_resolver,
        ),
        patch("api.plugins.github.launch.add_connectors_to_inputs", new=AsyncMock()),
        patch("api.plugins.github.launch.add_notify_to_inputs") as notify_mock,
    ):
        await build_dispatch_inputs(workflow, pr)

    assert notify_mock.called is expected
    if expected:
        assert notify_mock.call_args.kwargs["git_org_id"] == pr.repository.org_id
