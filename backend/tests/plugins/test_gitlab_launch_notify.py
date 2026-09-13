"""The gitlab launch path wires the notify list for the right workflows."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.models.executions import ExecutionWorkflow


def _make_mr() -> MagicMock:
    mr = MagicMock()
    mr.id = uuid4()
    mr.pr_url = "https://gitlab.example.com/group/app/-/merge_requests/42"
    mr.repository = MagicMock()
    mr.repository.org_id = uuid4()
    mr.repository.auth_token_encrypted = None
    return mr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (ExecutionWorkflow.REVIEW, True),
        (ExecutionWorkflow.RESPOND, True),
        (ExecutionWorkflow.SUMMARY, False),
    ],
)
async def test_notify_wired_only_for_loop_ending_workflows(workflow, expected):
    """Only REVIEW and RESPOND can emit the ready notice — they are the two
    ends of the review loop. A summary run has no business carrying the
    tenant's notify list into its container."""
    from api.plugins.container.utils import ThirdPartyPluginsResolved
    from api.plugins.gitlab.launch import build_dispatch_inputs

    mr = _make_mr()

    async def fake_resolver(org_id, *, workflow):
        return ThirdPartyPluginsResolved()

    with (
        patch("api.plugins.gitlab.launch.add_llm_to_inputs"),
        patch("api.plugins.gitlab.launch.add_gitlab_workspace_credentials", new=AsyncMock()),
        patch(
            "api.plugins.gitlab.launch.resolve_third_party_plugins_env_for_org",
            new=fake_resolver,
        ),
        patch("api.plugins.gitlab.launch.add_plugin_marketplace_credentials", new=AsyncMock()),
        patch("api.plugins.gitlab.launch.add_connectors_to_inputs", new=AsyncMock()),
        patch("api.plugins.gitlab.launch.add_notify_to_inputs") as notify_mock,
    ):
        await build_dispatch_inputs(workflow, mr)

    assert notify_mock.called is expected
    if expected:
        assert notify_mock.call_args.kwargs["git_org_id"] == mr.repository.org_id


@pytest.mark.asyncio
async def test_mention_driven_respond_container_wires_notify():
    """The mention path builds its own inputs rather than going through
    build_dispatch_inputs, so it needs the wiring in its own right — a
    resolve-only turn is one of the two places the notice can fire."""
    from api.plugins.gitlab.launch import launch_respond_container

    org_id = uuid4()
    backend = MagicMock()
    backend.start_container = AsyncMock(return_value="container-1")

    gitlab_plugin = MagicMock()
    gitlab_plugin._watcher = MagicMock()
    gitlab_plugin._watcher.backend = backend

    app = MagicMock()
    app.gitlab = gitlab_plugin
    app.database = None
    app.options = MagicMock()

    from api.plugins.container.utils import ThirdPartyPluginsResolved

    async def fake_resolver(org_id, *, workflow):
        return ThirdPartyPluginsResolved()

    with (
        patch("api.plugins.gitlab.launch.get_current_app", return_value=app),
        patch("api.plugins.gitlab.launch.add_llm_to_inputs"),
        patch("api.plugins.gitlab.launch._handle_llm_unavailable", return_value=False),
        patch("api.plugins.gitlab.launch.add_gitlab_workspace_credentials", new=AsyncMock()),
        patch(
            "api.plugins.gitlab.launch.resolve_third_party_plugins_env_for_org",
            new=fake_resolver,
        ),
        patch("api.plugins.gitlab.launch.add_plugin_marketplace_credentials", new=AsyncMock()),
        patch("api.plugins.gitlab.launch.add_connectors_to_inputs", new=AsyncMock()),
        patch("api.plugins.gitlab.launch.ContainerRequest"),
        patch("api.plugins.gitlab.launch.build_container_labels", return_value={}),
        patch("api.plugins.gitlab.launch.add_notify_to_inputs") as notify_mock,
    ):
        await launch_respond_container(
            target_url="https://gitlab.example.com/group/app/-/merge_requests/42#note_1",
            org_id=org_id,
            repo=None,
            execution_id=uuid4(),
        )

    assert notify_mock.called
    assert notify_mock.call_args.kwargs["git_org_id"] == org_id
