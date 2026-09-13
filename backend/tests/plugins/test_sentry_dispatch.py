"""Tests for Sentry dispatch loop."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.database.llm_credentials import LLMCredentialAvailability
from api.plugins.sentry.config import SentryDispatchConfig
from api.plugins.sentry.dispatch import SentryDispatcher


@pytest.fixture
def dispatch_config():
    return SentryDispatchConfig(
        enabled=True,
        interval_seconds=1,
        batch_size=5,
        max_retries=3,
        max_concurrent_dispatches=10,
    )


@pytest.fixture
def dispatcher(dispatch_config):
    return SentryDispatcher(dispatch_config)


@pytest.mark.asyncio
async def test_start_creates_task(dispatcher):
    """Start creates the background loop task."""
    with patch.object(dispatcher, "_run_loop", new_callable=AsyncMock):
        await dispatcher.start()
        assert dispatcher._task is not None
        assert not dispatcher._task.done()
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_stop_cancels_task(dispatcher):
    """Stop cancels the background task."""
    with patch.object(dispatcher, "_run_loop", new_callable=AsyncMock):
        await dispatcher.start()
        await dispatcher.stop()
        assert dispatcher._task is None
        assert dispatcher._stopping.is_set()


def _sync_run_in_session(mock_db):
    """A ``run_in_session`` stand-in that runs the closure against ``mock_db``."""
    return AsyncMock(side_effect=lambda fn: fn(mock_db))


@pytest.mark.asyncio
async def test_tick_no_eligible_targets():
    """Tick with no eligible (sentry_org, git_org) pairs does nothing."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1)
    dispatcher = SentryDispatcher(config)

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_eligible_dispatch_targets",
            return_value=[],
        ) as mock_eligible,
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
        ) as mock_claim,
    ):
        await dispatcher._tick()
        mock_eligible.assert_called_once()
        mock_claim.assert_not_called()


@pytest.mark.asyncio
async def test_tick_dispatches_for_eligible_target():
    """Tick claims the git-org window, grabs a pair-scoped batch, dispatches."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1)
    dispatcher = SentryDispatcher(config)

    sentry_org_id = uuid.uuid4()
    git_org_id = uuid.uuid4()
    issue_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    mock_issue = MagicMock()
    mock_issue.id = issue_id
    mock_issue.external_id = "12345"

    mock_execution = MagicMock()
    mock_execution.id = execution_id

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_sentry_org = MagicMock()
    mock_sentry_org.external_org_id = "myorg"
    mock_sentry_org.base_url = "https://sentry.io"
    mock_sentry_org.settings = {}

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    mock_launch = AsyncMock(return_value="container-123")
    mock_reconcile = AsyncMock()

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_eligible_dispatch_targets",
            return_value=[(sentry_org_id, git_org_id)],
        ),
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
            return_value=True,
        ) as mock_claim,
        patch(
            "api.plugins.sentry.dispatch.db_get_org_by_id",
            return_value=mock_sentry_org,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
            return_value=[mock_issue],
        ) as mock_batch,
        patch(
            "api.plugins.sentry.dispatch.db_create_execution",
            return_value=mock_execution,
        ) as mock_create_exec,
        patch("api.plugins.sentry.dispatch.launch_container", mock_launch),
        patch("api.plugins.sentry.dispatch.reconcile_open_fix_prs", mock_reconcile),
        patch(
            "api.plugins.sentry.dispatch.resolve_memory_workspace_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._tick()

    mock_create_exec.assert_called_once()
    # Window is claimed on the git org, batch is scoped to the pair.
    assert mock_claim.call_args.args[1] == git_org_id
    assert mock_batch.call_args.kwargs["git_org_id"] == git_org_id
    # Gate is off by default → no reconciliation.
    mock_reconcile.assert_not_called()
    mock_launch.assert_called_once_with(
        [mock_issue], mock_sentry_org, execution_id, workspace_id=None
    )


@pytest.mark.asyncio
async def test_claim_fails_skips_dispatch():
    """If claim_dispatch_window returns False, dispatch is skipped."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1)
    dispatcher = SentryDispatcher(config)

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_sentry_org = MagicMock()
    mock_sentry_org.settings = {}

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin
    mock_app.container = AsyncMock()

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_org_by_id",
            return_value=mock_sentry_org,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
            return_value=False,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
        ) as mock_batch,
    ):
        await dispatcher._dispatch_target(uuid.uuid4(), uuid.uuid4())
        mock_batch.assert_not_called()


@pytest.mark.asyncio
async def test_container_failure_marks_executions_failed():
    """When container dispatch fails, executions are marked as failed."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1, max_retries=3)
    dispatcher = SentryDispatcher(config)

    execution_id = uuid.uuid4()

    mock_issue = MagicMock()
    mock_issue.id = uuid.uuid4()
    mock_issue.external_id = "99999"

    mock_execution = MagicMock()
    mock_execution.id = execution_id

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_sentry_org = MagicMock()
    mock_sentry_org.external_org_id = "myorg"
    mock_sentry_org.settings = {}

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    mock_launch = AsyncMock(return_value=None)

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
            return_value=True,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_org_by_id",
            return_value=mock_sentry_org,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
            return_value=[mock_issue],
        ),
        patch(
            "api.plugins.sentry.dispatch.db_create_execution",
            return_value=mock_execution,
        ),
        patch("api.plugins.sentry.dispatch.launch_container", mock_launch),
        patch(
            "api.plugins.sentry.dispatch.resolve_memory_workspace_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._dispatch_target(uuid.uuid4(), uuid.uuid4())
        mock_launch.assert_called_once_with(
            [mock_issue], mock_sentry_org, execution_id, workspace_id=None
        )


@pytest.mark.asyncio
async def test_gate_enabled_reconciles_and_blocks_on_open_fix_pr():
    """With the gate on, a git org with an open fix PR reconciles then stands down."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1)
    dispatcher = SentryDispatcher(config)

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_sentry_org = MagicMock()
    mock_sentry_org.settings = {"gate_on_open_fix_prs": True}

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    mock_reconcile = AsyncMock()

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_org_by_id",
            return_value=mock_sentry_org,
        ),
        patch("api.plugins.sentry.dispatch.reconcile_open_fix_prs", mock_reconcile),
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
            return_value=True,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
            return_value=[MagicMock()],
        ),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_open_fix_pr",
            return_value=True,
        ),
        patch("api.plugins.sentry.dispatch.db_create_execution") as mock_create_exec,
        patch("api.plugins.sentry.dispatch.launch_container") as mock_launch,
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._dispatch_target(uuid.uuid4(), uuid.uuid4())

    mock_reconcile.assert_awaited_once()
    mock_create_exec.assert_not_called()
    mock_launch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_target_held_when_git_org_has_a_running_fix_batch():
    """The git org is the perimeter: a running fixer for it blocks its next batch,
    before the window is even claimed."""
    dispatcher = SentryDispatcher(SentryDispatchConfig(enabled=True, interval_seconds=1))

    mock_db = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_db)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)
    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    mock_sentry_org = MagicMock()
    mock_sentry_org.settings = {}

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch("api.plugins.sentry.dispatch.db_get_org_by_id", return_value=mock_sentry_org),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution",
            return_value=True,
        ),
        patch("api.plugins.sentry.dispatch.db_claim_dispatch_window") as mock_claim,
        patch("api.plugins.sentry.dispatch.db_create_execution") as mock_create_exec,
        patch("api.plugins.sentry.dispatch.launch_container") as mock_launch,
    ):
        await dispatcher._dispatch_target(uuid.uuid4(), uuid.uuid4())

    mock_claim.assert_not_called()
    mock_create_exec.assert_not_called()
    mock_launch.assert_not_called()


@pytest.mark.asyncio
async def test_tick_fans_out_over_multiple_git_orgs():
    """Different git orgs dispatch concurrently — the perimeter is per git org."""
    dispatcher = SentryDispatcher(SentryDispatchConfig(enabled=True, interval_seconds=1))

    mock_db = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_db)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)
    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    mock_sentry_org = MagicMock()
    mock_sentry_org.settings = {}
    mock_execution = MagicMock()
    mock_execution.id = uuid.uuid4()

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_get_eligible_dispatch_targets",
            return_value=[(uuid.uuid4(), uuid.uuid4()), (uuid.uuid4(), uuid.uuid4())],
        ),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution",
            return_value=False,
        ),
        patch("api.plugins.sentry.dispatch.db_get_org_by_id", return_value=mock_sentry_org),
        patch("api.plugins.sentry.dispatch.db_claim_dispatch_window", return_value=True),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
            return_value=[MagicMock()],
        ),
        patch(
            "api.plugins.sentry.dispatch.db_create_execution", return_value=mock_execution
        ) as mock_create_exec,
        patch(
            "api.plugins.sentry.dispatch.launch_container",
            AsyncMock(return_value="container-1"),
        ) as mock_launch,
        patch(
            "api.plugins.sentry.dispatch.resolve_memory_workspace_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._tick()

    assert mock_create_exec.call_count == 2
    assert mock_launch.await_count == 2


@pytest.mark.asyncio
async def test_all_stale_credentials_skips_execution_creation_entirely():
    """ADR-010: when every LLM credential is stale, the tick must not create
    an execution at all — that's what makes the issue naturally retryable
    on the next tick, with no new retry-count machinery needed."""
    config = SentryDispatchConfig(enabled=True, interval_seconds=1)
    dispatcher = SentryDispatcher(config)

    mock_issue = MagicMock()

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)

    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = _sync_run_in_session(mock_db)

    mock_sentry_org = MagicMock()
    mock_sentry_org.settings = {}
    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    retry_at = object()

    with (
        patch("api.plugins.sentry.dispatch.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.dispatch.db_git_org_has_running_fix_execution", return_value=False
        ),
        patch(
            "api.plugins.sentry.dispatch.db_claim_dispatch_window",
            return_value=True,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_org_by_id",
            return_value=mock_sentry_org,
        ),
        patch(
            "api.plugins.sentry.dispatch.db_get_dispatchable_issues",
            return_value=[mock_issue],
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(
                availability=LLMCredentialAvailability.ALL_STALE, retry_at=retry_at
            ),
        ),
        patch("api.plugins.sentry.dispatch.db_create_execution") as mock_create_exec,
        patch("api.plugins.sentry.dispatch.launch_container") as mock_launch,
    ):
        await dispatcher._dispatch_target(uuid.uuid4(), uuid.uuid4())

    mock_create_exec.assert_not_called()
    mock_launch.assert_not_called()


@pytest.mark.asyncio
async def test_build_dispatch_inputs_routes_secrets_to_sidecar() -> None:
    """Sentry token + LLM oauth go to secrets/upstreams; URL stays public."""
    from api.plugins.container.security_proxy import CredentialKey
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_sentry_plugin = MagicMock()
    mock_sentry_plugin.config.auth_token = "sntrys_test"
    mock_sentry_plugin.config.base_url = "https://sentry.io"

    mock_options = MagicMock()
    mock_options.claude_code.oauth_token = "cc-oauth-token"
    mock_options.claude_code.api_key = None

    mock_app = MagicMock()
    mock_app.sentry = mock_sentry_plugin
    mock_app.options = mock_options
    mock_app.database = None
    mock_app.github = None

    mock_issue = MagicMock()
    mock_issue.repository = None  # No repo linked

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = None
    mock_org.base_url = None

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
    ):
        inputs, _ = await build_dispatch_inputs([mock_issue], mock_org)

    # Credentials live on the sidecar, never on the agent.
    assert inputs.secrets[CredentialKey.SENTRY_AUTH_TOKEN] == "sntrys_test"
    assert inputs.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN] == "cc-oauth-token"
    assert "SENTRY_AUTH_TOKEN" not in inputs.public_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in inputs.public_env

    # URL hints stay public — agents need them to format requests.
    assert inputs.public_env["SENTRY_API_URL"] == "https://sentry.io"

    # Upstreams describe where the proxy can call. SaaS Sentry orgs get
    # all four regional hosts so the CLI can re-target after region discovery.
    hosts = {u.host for u in inputs.upstreams}
    assert hosts == {
        "sentry.io",
        "us.sentry.io",
        "de.sentry.io",
        "eu.sentry.io",
        "api.anthropic.com",
    }


@pytest.mark.asyncio
async def test_build_dispatch_inputs_extends_extra_hosts_with_third_party_plugin_hosts() -> None:
    """The launcher must allowlist every third-party plugin's git host so
    `git clone` of those plugins isn't denied at the proxy."""
    from api.plugins.container.utils import ThirdPartyPluginsResolved
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_sentry_plugin = MagicMock()
    mock_sentry_plugin.config.auth_token = "sntrys_test"
    mock_sentry_plugin.config.base_url = "https://sentry.io"

    mock_options = MagicMock()
    mock_options.claude_code.oauth_token = "cc-oauth-token"
    mock_options.claude_code.api_key = None

    mock_app = MagicMock()
    mock_app.sentry = mock_sentry_plugin
    mock_app.options = mock_options
    mock_app.database = None
    mock_app.github = None

    mock_issue = MagicMock()
    mock_issue.repository = None

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = None
    mock_org.base_url = None

    plugins_resolved = ThirdPartyPluginsResolved(
        env={"JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1"},
        extra_hosts=["bitbucket.org", "gitlab.com"],
    )

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.sentry.launch.resolve_third_party_plugins_env",
            new=AsyncMock(return_value=plugins_resolved),
        ),
    ):
        inputs, _ = await build_dispatch_inputs([mock_issue], mock_org)

    assert "bitbucket.org" in inputs.extra_hosts
    assert "gitlab.com" in inputs.extra_hosts
    # Existing tooling hosts must still be present.
    assert "github.com" in inputs.extra_hosts
    assert inputs.public_env["JEANCLODE_THIRDPARTY_PLUGINS_ENABLED"] == "1"


@pytest.mark.asyncio
async def test_build_dispatch_inputs_self_hosted_sentry_uses_org_url() -> None:
    """A self-hosted Sentry org sends its own host into the proxy allowlist."""
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_app = MagicMock()
    mock_app.sentry = None  # No plugin-level fallback
    mock_app.options.claude_code.oauth_token = None
    mock_app.options.claude_code.api_key = None
    mock_app.database = MagicMock()
    mock_app.database.decrypt = MagicMock(return_value="decrypted-sentry-token")
    mock_app.github = None

    mock_issue = MagicMock()
    mock_issue.repository = None

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = "encrypted"
    mock_org.base_url = "https://sentry.acme.example"

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.container.dispatch_inputs.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.NONE_CONFIGURED, None, None),
        ),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
    ):
        inputs, _ = await build_dispatch_inputs([mock_issue], mock_org)

    hosts = [u.host for u in inputs.upstreams]
    assert hosts == ["sentry.acme.example"]
    assert inputs.public_env["SENTRY_API_URL"] == "https://sentry.acme.example"
    assert inputs.secrets["SENTRY_AUTH_TOKEN"] == "decrypted-sentry-token"


def test_build_sentry_command_interleaves_repo_override() -> None:
    """Each issue's mapped_repo.web_url is passed as --repo right after its URL.

    Without this, the agent's CLI resolver would query Sentry's code-mappings
    and clone whatever Sentry says — which can disagree with the user's manual
    mapping in our DB (e.g. Sentry says github.com/Foo/bar but the workspace
    mapping points at gitlab.com/foo/bar).
    """
    from api.plugins.sentry.launch import build_sentry_command

    org = MagicMock()
    org.base_url = "https://sentry.io"
    org.external_org_id = "myorg"

    issue1 = MagicMock()
    issue1.external_id = "111"
    issue1.issue_url = None  # no permalink stored → synthesized URL
    issue1.repository.mapped_repo.web_url = "https://gitlab.com/foo/bar"

    issue2 = MagicMock()
    issue2.external_id = "222"
    issue2.issue_url = None
    issue2.repository.mapped_repo.web_url = "https://github.com/baz/qux"

    issue3 = MagicMock()
    issue3.external_id = "333"
    issue3.issue_url = None
    issue3.repository.mapped_repo = None  # No mapping → no --repo

    cmd = build_sentry_command([issue1, issue2, issue3], org)
    assert cmd == [
        "https://myorg.sentry.io/issues/111/",
        "--repo",
        "https://gitlab.com/foo/bar",
        "https://myorg.sentry.io/issues/222/",
        "--repo",
        "https://github.com/baz/qux",
        "https://myorg.sentry.io/issues/333/",
    ]


def test_build_sentry_command_uses_the_stored_permalink() -> None:
    """Sentry's own permalink wins over a synthesized URL.

    On self-hosted the real issue URL is org-scoped
    (/organizations/<org>/issues/<id>/); the flat <base>/issues/<id>/ we
    synthesize is wrong and gets quoted verbatim in every PR/MR body the
    workflow writes.
    """
    from api.plugins.sentry.launch import build_sentry_command

    org = MagicMock()
    org.base_url = "https://sentry.example.com"
    org.external_org_id = "examplecorp"

    issue = MagicMock()
    issue.external_id = "1887793"
    issue.issue_url = "https://sentry.example.com/organizations/examplecorp/issues/1887793/"
    issue.repository.mapped_repo.web_url = "https://gitlab.example/org/app"

    cmd = build_sentry_command([issue], org)
    assert cmd[0] == "https://sentry.example.com/organizations/examplecorp/issues/1887793/"


def test_build_sentry_command_falls_back_when_no_permalink_stored() -> None:
    from api.plugins.sentry.launch import build_sentry_command

    org = MagicMock()
    org.base_url = "https://sentry.example.com"
    org.external_org_id = "examplecorp"

    issue = MagicMock()
    issue.external_id = "1887793"
    issue.issue_url = None
    issue.repository.mapped_repo = None

    cmd = build_sentry_command([issue], org)
    assert cmd == ["https://sentry.example.com/issues/1887793/"]


def test_build_sentry_command_appends_related_repo_after_primary_repo() -> None:
    """--related-repo flags follow --repo, staying bound to this issue's URL —
    the CLI parser binds trailing flags to whatever URL(s) precede them."""
    from api.plugins.sentry.launch import build_sentry_command

    org = MagicMock()
    org.base_url = "https://sentry.io"
    org.external_org_id = "myorg"

    issue = MagicMock()
    issue.external_id = "111"
    issue.issue_url = None
    issue.repository.mapped_repo.web_url = "https://github.com/org/primary"

    related_a = MagicMock()
    related_a.web_url = "https://github.com/org/shared-lib"
    related_b = MagicMock()
    related_b.web_url = "https://github.com/org/other-service"

    cmd = build_sentry_command([issue], org, related_repos=[related_a, related_b])
    assert cmd == [
        "https://myorg.sentry.io/issues/111/",
        "--repo",
        "https://github.com/org/primary",
        "--related-repo",
        "https://github.com/org/shared-lib",
        "--related-repo",
        "https://github.com/org/other-service",
    ]


def test_resolve_related_repos_returns_empty_without_mapped_repo() -> None:
    from api.plugins.sentry.launch import _resolve_related_repos

    mock_app = MagicMock()
    mock_app.database = MagicMock()

    issue = MagicMock()
    issue.repository = None

    with patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app):
        assert _resolve_related_repos([issue]) == []


def test_resolve_related_repos_queries_by_mapped_repo_id() -> None:
    from api.plugins.sentry.launch import _resolve_related_repos

    mapped_repo = MagicMock()
    mapped_repo.id = uuid.uuid4()

    issue = MagicMock()
    issue.repository.mapped_repo = mapped_repo

    mock_db = MagicMock()
    mock_session_ctx = MagicMock()
    mock_session_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_session_ctx.__exit__ = MagicMock(return_value=False)
    mock_db_plugin = MagicMock()
    mock_db_plugin.session.return_value = mock_session_ctx
    mock_db_plugin.run_in_session = AsyncMock(side_effect=lambda fn: fn(mock_db))

    mock_app = MagicMock()
    mock_app.database = mock_db_plugin

    related = [MagicMock()]
    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.sentry.launch.db_get_related_repos", return_value=related) as mock_query,
    ):
        result = _resolve_related_repos([issue])

    assert result == related
    mock_query.assert_called_once_with(mock_db, mapped_repo.id)


@pytest.mark.asyncio
async def test_build_dispatch_inputs_uses_gitlab_workspace_credentials_when_related_repo_crosses_org() -> (
    None
):
    """A related repo in a different GitLab org needs the workspace-wide,
    path-prefix-scoped credential path — the single-org path only carries a
    token for the primary repo's own group."""
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_app = MagicMock()
    mock_app.sentry = None
    mock_app.options.claude_code.oauth_token = None
    mock_app.options.claude_code.api_key = None
    mock_app.database = MagicMock()
    mock_app.github = None

    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    mapped_repo = MagicMock()
    mapped_repo.provider = "gitlab"
    mapped_repo.org_id = org_a
    mapped_repo.auth_token_encrypted = "enc"

    related_repo = MagicMock()
    related_repo.org_id = org_b

    mock_issue = MagicMock()
    mock_issue.repository.mapped_repo = mapped_repo

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = None
    mock_org.base_url = None

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.container.dispatch_inputs.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.NONE_CONFIGURED, None, None),
        ),
        patch(
            "api.plugins.sentry.launch.add_gitlab_workspace_credentials", new=AsyncMock()
        ) as mock_workspace,
        patch(
            "api.plugins.sentry.launch.add_git_platform_to_inputs", new=AsyncMock()
        ) as mock_single,
    ):
        await build_dispatch_inputs([mock_issue], mock_org, related_repos=[related_repo])

    mock_workspace.assert_called_once()
    mock_single.assert_not_called()
    assert mock_workspace.call_args.kwargs["git_org_id"] == org_a


@pytest.mark.asyncio
async def test_build_dispatch_inputs_uses_single_org_credentials_when_related_repo_same_org() -> (
    None
):
    """A related repo in the SAME GitLab org doesn't need the workspace-wide
    lookup — the primary org's token already covers it, so stick with the
    cheaper single-org path."""
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_app = MagicMock()
    mock_app.sentry = None
    mock_app.options.claude_code.oauth_token = None
    mock_app.options.claude_code.api_key = None
    mock_app.database = MagicMock()
    mock_app.github = None

    org_a = uuid.uuid4()

    mapped_repo = MagicMock()
    mapped_repo.provider = "gitlab"
    mapped_repo.org_id = org_a
    mapped_repo.auth_token_encrypted = "enc"

    related_repo_same_org = MagicMock()
    related_repo_same_org.org_id = org_a

    mock_issue = MagicMock()
    mock_issue.repository.mapped_repo = mapped_repo

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = None
    mock_org.base_url = None

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.container.dispatch_inputs.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.NONE_CONFIGURED, None, None),
        ),
        patch(
            "api.plugins.sentry.launch.add_gitlab_workspace_credentials", new=AsyncMock()
        ) as mock_workspace,
        patch(
            "api.plugins.sentry.launch.add_git_platform_to_inputs", new=AsyncMock()
        ) as mock_single,
    ):
        await build_dispatch_inputs([mock_issue], mock_org, related_repos=[related_repo_same_org])

    mock_single.assert_called_once()
    mock_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_build_dispatch_inputs_uses_single_org_credentials_for_github_regardless_of_related_repos() -> (
    None
):
    """GitHub App installation tokens already cover every repo under that
    installation, so a related repo in a different org doesn't need the
    (GitLab-only) workspace-wide credential path."""
    from api.plugins.sentry.launch import build_dispatch_inputs

    mock_app = MagicMock()
    mock_app.sentry = None
    mock_app.options.claude_code.oauth_token = None
    mock_app.options.claude_code.api_key = None
    mock_app.database = MagicMock()
    mock_app.github = None

    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    mapped_repo = MagicMock()
    mapped_repo.provider = "github"
    mapped_repo.org_id = org_a
    mapped_repo.auth_token_encrypted = None

    related_repo = MagicMock()
    related_repo.org_id = org_b

    mock_issue = MagicMock()
    mock_issue.repository.mapped_repo = mapped_repo

    mock_org = MagicMock()
    mock_org.auth_token_encrypted = None
    mock_org.base_url = None

    with (
        patch("api.plugins.sentry.launch.get_current_app", return_value=mock_app),
        patch("api.plugins.container.utils.get_current_app", return_value=mock_app),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=mock_app),
        patch(
            "api.plugins.container.dispatch_inputs.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.NONE_CONFIGURED, None, None),
        ),
        patch(
            "api.plugins.sentry.launch.add_gitlab_workspace_credentials", new=AsyncMock()
        ) as mock_workspace,
        patch(
            "api.plugins.sentry.launch.add_git_platform_to_inputs", new=AsyncMock()
        ) as mock_single,
    ):
        await build_dispatch_inputs([mock_issue], mock_org, related_repos=[related_repo])

    mock_single.assert_called_once()
    mock_workspace.assert_not_called()


def test_build_sentry_command_self_hosted_uses_org_base_url() -> None:
    """Self-hosted Sentry: URL prefix is the org's base_url, not the slug subdomain."""
    from api.plugins.sentry.launch import build_sentry_command

    org = MagicMock()
    org.base_url = "https://sentry.acme.example"
    org.external_org_id = "myorg"

    issue = MagicMock()
    issue.external_id = "42"
    issue.issue_url = None  # fallback path; the permalink one is covered above
    issue.repository = None

    cmd = build_sentry_command([issue], org)
    assert cmd == ["https://sentry.acme.example/issues/42/"]


@pytest.mark.asyncio
async def test_health_check_healthy(dispatcher):
    """Health check reports healthy when task is running."""
    with patch.object(dispatcher, "_run_loop", new_callable=AsyncMock):
        await dispatcher.start()
        result = dispatcher.health_check()
        assert result["healthy"] is True
        assert result["stopping"] is False
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_health_check_not_started(dispatcher):
    """Health check reports unhealthy when not started."""
    result = dispatcher.health_check()
    assert result["healthy"] is False


# ── Real-DB integration: the whole dispatch path executes against Postgres ──


@pytest.mark.asyncio
async def test_dispatch_target_end_to_end_against_real_db(app, db_session, dispatcher):
    """Exercise db_get_eligible_dispatch_targets → db_claim_dispatch_window →
    db_get_dispatchable_issues → db_create_execution against real Postgres, so
    a malformed query or the int-minutes make_interval can't slip through
    behind the mocked unit tests."""
    from api.database.execution import db_get_eligible_dispatch_targets
    from api.database.repository import db_update_repository_mapping
    from api.models.executions import Execution
    from api.models.issues import Issue, TriageResult
    from api.models.organizations import Organization
    from api.models.repositories import MappingMethod, Repository
    from api.models.workspaces import Workspace

    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db_session.add(ws)
    db_session.flush()
    sentry_org = Organization(
        workspace_id=ws.id,
        name="s",
        external_org_id=f"s-{uuid.uuid4().hex[:6]}",
        provider="sentry",
        settings={"triggers": {"triage": "automatic"}, "batch_size": "3", "batch_window": "1h"},
    )
    git_org = Organization(
        workspace_id=ws.id,
        name="g",
        external_org_id=f"g-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db_session.add_all([sentry_org, git_org])
    db_session.commit()

    git_repo = Repository(
        org_id=git_org.id,
        name="acme/app",
        external_id=f"gr-{uuid.uuid4().hex[:6]}",
        provider="github",
    )
    db_session.add(git_repo)
    db_session.commit()
    sentry_repo = Repository(
        org_id=sentry_org.id,
        name="app",
        external_id=f"sp-{uuid.uuid4().hex[:6]}",
        provider="sentry",
    )
    db_session.add(sentry_repo)
    db_session.commit()
    db_update_repository_mapping(
        db_session, sentry_repo.id, git_repo.id, MappingMethod.MANUAL.value
    )
    for i in range(5):
        db_session.add(
            Issue(
                repository_id=sentry_repo.id,
                external_id=f"e-{i}-{uuid.uuid4().hex[:4]}",
                title=f"boom {i}",
                level="error",
                triage_result=TriageResult.PENDING.value,
            )
        )
    db_session.commit()

    targets = db_get_eligible_dispatch_targets(db_session, provider="sentry", max_retries=3)
    assert (sentry_org.id, git_org.id) in targets

    captured = {}

    async def fake_launch(issues, org, execution_id, *, workspace_id=None):
        captured["n"] = len(issues)
        captured["org_id"] = org.id
        captured["execution_id"] = execution_id
        return "container-x"

    with (
        patch("api.plugins.sentry.dispatch.launch_container", fake_launch),
        patch(
            "api.plugins.sentry.dispatch.resolve_memory_workspace_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._dispatch_target(sentry_org.id, git_org.id)

    # batch_size "3" honoured, one execution created and linked, window claimed.
    assert captured["n"] == 3
    assert captured["org_id"] == sentry_org.id
    execs = db_session.query(Execution).all()
    assert len(execs) == 1 and execs[0].workflow == "fix"
    assert {i.external_id for i in execs[0].issues} <= {
        i.external_id for i in db_session.query(Issue).all()
    }
    db_session.refresh(git_org)
    assert git_org.last_dispatched_at is not None

    # Second call is a no-op — window still held.
    l2 = AsyncMock(return_value="container-y")
    with (
        patch("api.plugins.sentry.dispatch.launch_container", l2),
        patch(
            "api.plugins.sentry.dispatch.resolve_memory_workspace_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "api.plugins.sentry.dispatch.check_llm_availability",
            return_value=MagicMock(availability=LLMCredentialAvailability.AVAILABLE),
        ),
    ):
        await dispatcher._dispatch_target(sentry_org.id, git_org.id)
    l2.assert_not_called()
    assert db_session.query(Execution).count() == 1
