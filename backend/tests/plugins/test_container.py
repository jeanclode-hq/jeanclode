"""Tests for the ContainerPlugin and DockerBackend."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.plugins.container.backend import (
    ContainerRequest,
    ContainerStatus,
)
from api.plugins.container.config import ContainerPluginConfig, DockerConfig
from api.plugins.container.docker import DockerBackend
from api.plugins.container.plugin import ContainerPlugin
from api.plugins.container.utils import parse_memory_limit


def _make_config(**overrides: object) -> ContainerPluginConfig:
    defaults = {"enabled": True, "backend": "docker", "docker": DockerConfig()}
    defaults.update(overrides)  # type: ignore[arg-type]
    return ContainerPluginConfig(**defaults)  # type: ignore[arg-type]


def _make_request(**overrides: object) -> ContainerRequest:
    defaults = {
        "image": "test-image:latest",
        "command": ["echo", "hello"],
        "env": {"FOO": "bar"},
        "timeout_seconds": 60,
        "labels": {"test": "true"},
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    return ContainerRequest(**defaults)  # type: ignore[arg-type]


def _make_backend(config: DockerConfig | None = None) -> DockerBackend:
    """Create a DockerBackend with mocked broker and redis."""
    broker = AsyncMock()
    redis = AsyncMock()
    return DockerBackend("test", config or DockerConfig(), broker, redis)


# ---------------------------------------------------------------------------
# Plugin lifecycle tests
# ---------------------------------------------------------------------------


async def test_startup_is_config_only() -> None:
    plugin = ContainerPlugin(_make_config())
    await plugin.startup()
    assert plugin.config.backend == "docker"


@pytest.mark.parametrize("backend_type", ["docker", "kubernetes"])
def test_backend_selection(backend_type: str) -> None:
    config = _make_config(backend=backend_type)
    plugin = ContainerPlugin(config)
    assert plugin.config.backend == backend_type


# ---------------------------------------------------------------------------
# DockerBackend.run() tests
# ---------------------------------------------------------------------------


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_success(mock_docker_cls: MagicMock) -> None:
    """Container runs, exits 0, stdout/stderr collected, cleanup happens."""
    mock_container = AsyncMock()
    mock_container.id = "abc123def456"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=["output line\n"])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    result = await backend.run(_make_request())

    assert result.container_id == "abc123def456"
    assert result.exit_code == 0
    assert result.status == ContainerStatus.COMPLETED
    assert result.duration_seconds >= 0
    mock_container.start.assert_awaited_once()
    mock_container.wait.assert_awaited_once()
    mock_container.delete.assert_awaited_once()


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_failure(mock_docker_cls: MagicMock) -> None:
    """Container exits non-zero → status FAILED."""
    mock_container = AsyncMock()
    mock_container.id = "fail123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 1})
    mock_container.log = AsyncMock(return_value=["error output\n"])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    result = await backend.run(_make_request())

    assert result.exit_code == 1
    assert result.status == ContainerStatus.FAILED


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_timeout(mock_docker_cls: MagicMock) -> None:
    """Container exceeds timeout → killed, status TIMEOUT."""
    mock_container = AsyncMock()
    mock_container.id = "timeout123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(side_effect=TimeoutError)
    mock_container.kill = AsyncMock()
    mock_container.log = AsyncMock(return_value=["partial output\n"])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    result = await backend.run(_make_request(timeout_seconds=1))

    assert result.status == ContainerStatus.TIMEOUT
    assert result.exit_code == -1
    mock_container.kill.assert_awaited_once()


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_cleans_up_on_wait_exception(mock_docker_cls: MagicMock) -> None:
    """Non-timeout exception during wait still triggers cleanup."""
    mock_container = AsyncMock()
    mock_container.id = "leak123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(side_effect=ConnectionError("daemon lost"))
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    with pytest.raises(ConnectionError):
        await backend.run(_make_request())

    mock_container.delete.assert_awaited_once()


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_collects_stdout_and_stderr(mock_docker_cls: MagicMock) -> None:
    """stdout and stderr are collected separately."""
    mock_container = AsyncMock()
    mock_container.id = "logs123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.delete = AsyncMock()

    async def mock_log(stdout=False, stderr=False, follow=False):
        if stdout and not stderr:
            return ["stdout line 1\n", "stdout line 2\n"]
        if stderr and not stdout:
            return ["stderr line 1\n"]
        return []

    mock_container.log = AsyncMock(side_effect=mock_log)

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    result = await backend.run(_make_request())

    assert result.stdout == "stdout line 1\nstdout line 2\n"
    assert result.stderr == "stderr line 1\n"


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_run_no_cleanup_when_disabled(mock_docker_cls: MagicMock) -> None:
    """Container is NOT deleted when cleanup_containers is False."""
    mock_container = AsyncMock()
    mock_container.id = "keep123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()
    mock_docker_cls.return_value = mock_client

    config = DockerConfig(cleanup_containers=False)
    backend = _make_backend(config)
    backend._docker = mock_client

    await backend.run(_make_request())

    mock_container.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Image pull tests
# ---------------------------------------------------------------------------


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_image_pulled_when_not_present(mock_docker_cls: MagicMock) -> None:
    """Image is pulled when inspect raises DockerError."""
    import aiodocker.exceptions

    mock_container = AsyncMock()
    mock_container.id = "pull123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(404, {"message": "not found"})
    )
    mock_client.images.pull = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    await backend.run(_make_request(image="new-image:latest"))

    mock_client.images.pull.assert_awaited_once_with("new-image:latest")


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_image_inspect_non_404_error_raises(mock_docker_cls: MagicMock) -> None:
    """Non-404 DockerError from image inspect is re-raised."""
    import aiodocker.exceptions

    mock_client = AsyncMock()
    mock_client.images.inspect = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(500, {"message": "internal error"})
    )
    mock_client.images.pull = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    with pytest.raises(aiodocker.exceptions.DockerError):
        await backend.run(_make_request())

    mock_client.images.pull.assert_not_awaited()


@patch("api.plugins.container.docker.aiodocker.Docker")
async def test_image_not_pulled_when_present(mock_docker_cls: MagicMock) -> None:
    """Image is NOT pulled when inspect succeeds."""
    mock_container = AsyncMock()
    mock_container.id = "exists123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock(return_value={"Id": "sha256:abc"})
    mock_client.images.pull = AsyncMock()
    mock_docker_cls.return_value = mock_client

    backend = _make_backend()
    backend._docker = mock_client

    await backend.run(_make_request())

    mock_client.images.pull.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_status tests
# ---------------------------------------------------------------------------


async def test_get_status_running() -> None:
    mock_container = AsyncMock()
    mock_container.show = AsyncMock(return_value={"State": {"Running": True}})

    mock_client = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)

    backend = _make_backend()
    backend._docker = mock_client

    status = await backend.get_status("container-123")
    assert status == ContainerStatus.RUNNING


async def test_get_status_completed() -> None:
    mock_container = AsyncMock()
    mock_container.show = AsyncMock(return_value={"State": {"Running": False, "ExitCode": 0}})

    mock_client = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)

    backend = _make_backend()
    backend._docker = mock_client

    status = await backend.get_status("container-123")
    assert status == ContainerStatus.COMPLETED


async def test_get_status_failed() -> None:
    mock_container = AsyncMock()
    mock_container.show = AsyncMock(return_value={"State": {"Running": False, "ExitCode": 1}})

    mock_client = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)

    backend = _make_backend()
    backend._docker = mock_client

    status = await backend.get_status("container-123")
    assert status == ContainerStatus.FAILED


async def test_get_status_oom_killed() -> None:
    mock_container = AsyncMock()
    mock_container.show = AsyncMock(
        return_value={"State": {"Running": False, "ExitCode": 0, "OOMKilled": True}}
    )

    mock_client = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)

    backend = _make_backend()
    backend._docker = mock_client

    status = await backend.get_status("container-123")
    assert status == ContainerStatus.FAILED


# ---------------------------------------------------------------------------
# get_logs tests
# ---------------------------------------------------------------------------


async def test_get_logs() -> None:
    mock_container = AsyncMock()
    mock_container.log = AsyncMock(return_value=["line 1\n", "line 2\n"])

    mock_client = AsyncMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)

    backend = _make_backend()
    backend._docker = mock_client

    logs = await backend.get_logs("container-123")
    assert logs == "line 1\nline 2\n"
    mock_container.log.assert_awaited_once_with(stdout=True, stderr=True, follow=False)


# ---------------------------------------------------------------------------
# stop_container tests
# ---------------------------------------------------------------------------


async def test_stop_container() -> None:
    mock_container = AsyncMock()
    mock_container.stop = AsyncMock()
    mock_container.show = AsyncMock(
        return_value={"Config": {"Labels": {"jeanclode.proxy_container": "jc-test-x-proxy"}}}
    )

    proxy_container = AsyncMock()
    mock_client = AsyncMock()
    mock_client.containers.get = AsyncMock(side_effect=[mock_container, proxy_container])

    backend = _make_backend()
    backend._docker = mock_client

    await backend.stop_container("container-123456")
    mock_container.stop.assert_awaited_once()
    # sidecar stopped too
    proxy_container.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# start_container (fire-and-forget) tests
# ---------------------------------------------------------------------------


def _sandbox_docker_mock() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Mock aiodocker client for the two-container sandbox launch.

    Returns ``(client, proxy_container, agent_container)``. ``containers.get``
    raises 404 so the pre-launch purge is a no-op.
    """
    import aiodocker.exceptions

    proxy = AsyncMock()
    proxy.id = "proxy123456789"
    proxy.show = AsyncMock(
        return_value={"State": {"Running": True, "Health": {"Status": "healthy"}}}
    )
    agent = AsyncMock()
    agent.id = "agent123456789"

    client = MagicMock()
    client.containers.create = AsyncMock(side_effect=[proxy, agent])
    client.containers.get = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(404, {"message": "no such container"})
    )
    client.images.inspect = AsyncMock()
    client.volumes.create = AsyncMock()
    client.volumes.get = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(404, {"message": "no such volume"})
    )
    return client, proxy, agent


async def test_start_container_launches_sandboxed_pair() -> None:
    client, proxy, agent = _sandbox_docker_mock()

    mock_redis = AsyncMock()
    mock_redis.sadd = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = DockerBackend("test", DockerConfig(), mock_broker, mock_redis)
    backend._docker = client

    container_id = await backend.start_container("exec-123", _make_request(secrets={}))

    assert container_id == "agent123456789"
    proxy.start.assert_awaited_once()
    agent.start.assert_awaited_once()
    # CA / config streamed into the sandbox volume via the sidecar
    proxy.put_archive.assert_awaited_once()
    agent.put_archive.assert_not_awaited()
    # per-execution tmp + sandbox volumes created
    assert client.volumes.create.await_count == 2
    mock_redis.sadd.assert_awaited_once()

    proxy_name = client.containers.create.await_args_list[0].kwargs["name"]
    agent_name = client.containers.create.await_args_list[1].kwargs["name"]
    assert proxy_name == "jc-test-exec-123-proxy"
    assert agent_name == "jc-test-exec-123"


async def test_start_container_agent_has_no_credentials() -> None:
    client, _proxy, _agent = _sandbox_docker_mock()
    backend = DockerBackend("test", DockerConfig(), AsyncMock(), AsyncMock())
    backend._docker = client

    request = _make_request(
        env={"FOO": "bar"},
        secrets={"ANTHROPIC_API_KEY": "sk-secret", "GH_TOKEN": "ghp_secret"},
    )
    await backend.start_container("exec-9", request)

    proxy_cfg = client.containers.create.await_args_list[0].kwargs["config"]
    agent_cfg = client.containers.create.await_args_list[1].kwargs["config"]

    proxy_env = dict(e.split("=", 1) for e in proxy_cfg["Env"])
    agent_env = dict(e.split("=", 1) for e in agent_cfg["Env"])

    # Real secret values live only on the sidecar
    assert proxy_env["ANTHROPIC_API_KEY"] == "sk-secret"
    assert proxy_env["GH_TOKEN"] == "ghp_secret"
    # Agent sees placeholders + proxy wiring, never the real values
    assert agent_env["ANTHROPIC_API_KEY"] == "sandbox-proxy-injected"
    assert agent_env["GH_TOKEN"] == "sandbox-proxy-injected"
    assert agent_env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert agent_env["NODE_EXTRA_CA_CERTS"] == "/sandbox/ca.crt"
    assert "ANTHROPIC_API_KEY=sk-secret" not in agent_cfg["Env"]

    assert agent_cfg["HostConfig"]["NetworkMode"] == "container:proxy123456789"
    assert agent_cfg["HostConfig"]["ReadonlyRootfs"] is True
    # sandbox volume is read-only on the agent, read-write on the sidecar
    assert (
        f"{backend._sandbox_volume_name('exec-9')}:/sandbox:ro" in agent_cfg["HostConfig"]["Binds"]
    )
    assert f"{backend._sandbox_volume_name('exec-9')}:/sandbox" in proxy_cfg["HostConfig"]["Binds"]
    assert proxy_cfg["HostConfig"]["CapDrop"] == ["ALL"]
    assert proxy_cfg["HostConfig"]["ReadonlyRootfs"] is True


async def test_start_container_cleans_up_on_proxy_failure() -> None:
    client, proxy, _agent = _sandbox_docker_mock()
    proxy.show = AsyncMock(return_value={"State": {"Running": False, "ExitCode": 1}})
    proxy.log = AsyncMock(return_value=["boom\n"])

    backend = DockerBackend("test", DockerConfig(), AsyncMock(), AsyncMock())
    backend._docker = client

    with pytest.raises(RuntimeError, match="security-proxy"):
        await backend.start_container("exec-x", _make_request(secrets={}))

    # purge runs twice: once before launch, once on the failure path
    assert client.containers.get.await_count >= 2


# ---------------------------------------------------------------------------
# _cleanup_execution tests
# ---------------------------------------------------------------------------


async def test_cleanup_execution_removes_pair_and_volumes_when_enabled() -> None:
    """cleanup_containers=True (prod default): agent, proxy and both volumes are removed."""
    import aiodocker.exceptions

    agent = AsyncMock()
    proxy = AsyncMock()
    client = MagicMock()
    client.containers.get = AsyncMock(side_effect=[agent, proxy])
    client.volumes.get = AsyncMock(
        side_effect=aiodocker.exceptions.DockerError(404, {"message": "no such volume"})
    )

    backend = _make_backend(DockerConfig(cleanup_containers=True))
    backend._docker = client

    await backend._cleanup_execution("exec-cleanup-1")

    agent.delete.assert_awaited_once_with(force=True)
    proxy.delete.assert_awaited_once_with(force=True)


async def test_cleanup_execution_stops_proxy_when_cleanup_disabled() -> None:
    """cleanup_containers=False (QA): the agent already exited on its own, but the
    security-proxy sidecar runs mitmdump forever and never exits by itself — it must
    still be *stopped* (not removed) so a completed execution doesn't leak a live,
    network-reachable container indefinitely. Regression for a QA run where every
    finished execution left its proxy container running (healthy) forever.
    """
    proxy = AsyncMock()
    client = MagicMock()
    client.containers.get = AsyncMock(return_value=proxy)

    backend = _make_backend(DockerConfig(cleanup_containers=False))
    backend._docker = client

    await backend._cleanup_execution("exec-cleanup-2")

    client.containers.get.assert_any_call(backend._proxy_name("exec-cleanup-2"))
    proxy.stop.assert_awaited_once()
    proxy.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Container config building tests
# ---------------------------------------------------------------------------


async def test_run_builds_correct_host_config() -> None:
    """Verify memory, CPU, and network are set in HostConfig."""
    mock_container = AsyncMock()
    mock_container.id = "cfg123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()

    config = DockerConfig(memory_limit="1g", cpu_limit=1.5, network="test-net")
    backend = _make_backend(config)
    backend._docker = mock_client

    await backend.run(_make_request())

    create_call = mock_client.containers.create.call_args
    host_config = create_call.kwargs["config"]["HostConfig"]
    assert host_config["Memory"] == 1024**3
    assert host_config["NanoCpus"] == int(1.5 * 1e9)
    assert host_config["NetworkMode"] == "test-net"


async def test_run_omits_cmd_when_empty() -> None:
    mock_container = AsyncMock()
    mock_container.id = "nocmd123"
    mock_container.start = AsyncMock()
    mock_container.wait = AsyncMock(return_value={"StatusCode": 0})
    mock_container.log = AsyncMock(return_value=[])
    mock_container.delete = AsyncMock()

    mock_client = AsyncMock()
    mock_client.containers.create = AsyncMock(return_value=mock_container)
    mock_client.images.inspect = AsyncMock()

    backend = _make_backend()
    backend._docker = mock_client

    await backend.run(_make_request(command=[]))

    create_call = mock_client.containers.create.call_args
    assert "Cmd" not in create_call.kwargs["config"]


# ---------------------------------------------------------------------------
# Connection management tests
# ---------------------------------------------------------------------------


async def test_docker_client_with_tcp_socket() -> None:
    """TCP socket URLs are used as-is (not prefixed with unix://)."""
    config = DockerConfig(socket="tcp://docker-proxy:2375")
    backend = _make_backend(config)

    with patch("api.plugins.container.docker.aiodocker.Docker") as mock_docker_cls:
        mock_client = AsyncMock()
        mock_docker_cls.return_value = mock_client
        await backend._get_docker()
        mock_docker_cls.assert_called_once_with(url="tcp://docker-proxy:2375")


async def test_docker_client_with_unix_socket() -> None:
    """Unix socket paths get unix:// prefix."""
    config = DockerConfig(socket="/var/run/docker.sock")
    backend = _make_backend(config)

    with patch("api.plugins.container.docker.aiodocker.Docker") as mock_docker_cls:
        mock_client = AsyncMock()
        mock_docker_cls.return_value = mock_client
        await backend._get_docker()
        mock_docker_cls.assert_called_once_with(url="unix:///var/run/docker.sock")


# ---------------------------------------------------------------------------
# parse_memory_limit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2g", 2 * 1024**3),
        ("512m", 512 * 1024**2),
        ("1024k", 1024 * 1024),
        ("100b", 100),
        ("1073741824", 1073741824),
    ],
)
def test_parse_memory_limit(value: str, expected: int) -> None:
    assert parse_memory_limit(value) == expected


# ---------------------------------------------------------------------------
# Redis tracking tests
# ---------------------------------------------------------------------------


async def test_register_execution_first_time() -> None:
    """First registration returns True and publishes status."""
    mock_redis = AsyncMock()
    mock_redis.sadd = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = DockerBackend("sentry", DockerConfig(), mock_broker, mock_redis)

    result = await backend.register_execution("exec-1", container_id="c-1")

    assert result is True
    mock_redis.sadd.assert_awaited_once_with("jeanclode:sentry:executions:active", "exec-1")
    mock_broker.publish.assert_awaited_once()


async def test_register_execution_duplicate() -> None:
    """Second registration returns False and does not publish."""
    mock_redis = AsyncMock()
    mock_redis.sadd = AsyncMock(return_value=0)
    mock_broker = AsyncMock()

    backend = DockerBackend("sentry", DockerConfig(), mock_broker, mock_redis)

    result = await backend.register_execution("exec-1")

    assert result is False
    mock_broker.publish.assert_not_awaited()


async def test_publish_status_idempotent_removes_from_active_set() -> None:
    """Idempotent publish uses SREM as gate for terminal statuses."""
    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=1)
    mock_broker = AsyncMock()

    backend = DockerBackend("sentry", DockerConfig(), mock_broker, mock_redis)

    result = await backend.publish_status_idempotent(
        execution_id="exec-1",
        status="completed",
    )

    assert result is True
    mock_redis.srem.assert_awaited_once_with("jeanclode:sentry:executions:active", "exec-1")
    mock_broker.publish.assert_awaited_once()


async def test_publish_status_idempotent_skips_already_processed() -> None:
    """If SREM returns 0, status was already processed — skip."""
    mock_redis = AsyncMock()
    mock_redis.srem = AsyncMock(return_value=0)
    mock_broker = AsyncMock()

    backend = DockerBackend("sentry", DockerConfig(), mock_broker, mock_redis)

    result = await backend.publish_status_idempotent(
        execution_id="exec-1",
        status="completed",
    )

    assert result is False
    mock_broker.publish.assert_not_awaited()


async def test_scoped_redis_keys() -> None:
    """Redis keys are scoped by plugin name."""
    backend = DockerBackend("sentry", DockerConfig(), AsyncMock(), AsyncMock())
    assert backend.active_executions_key == "jeanclode:sentry:executions:active"
    assert backend.reconcile_lock_key == "jeanclode:sentry:reconcile:lock"
    assert backend.status_stream == "jeanclode.sentry.execution.status"

    backend2 = DockerBackend("predecessor", DockerConfig(), AsyncMock(), AsyncMock())
    assert backend2.active_executions_key == "jeanclode:predecessor:executions:active"
    assert backend2.status_stream == "jeanclode.predecessor.execution.status"
