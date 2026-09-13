"""Docker backend for container execution and watching.

Every dispatched execution is sandboxed, exactly as on Kubernetes: the agent
container holds no credentials and is pointed at a security-proxy sidecar
that owns every secret and is the only process with internet egress. The
two containers share one network namespace (the agent joins the proxy's via
``NetworkMode=container:<id>``), which is the Docker equivalent of a K8s
pod's shared loopback — so ``HTTPS_PROXY=http://127.0.0.1:8080`` reaches the
proxy and nothing else on the host can.

Both containers run read-only-rootfs. Per execution there are two ``local``
volumes: ``-tmp`` (the agent's ``/tmp`` scratch) and ``-sandbox`` (the CA
cert/key + proxy config — what K8s delivers as Secret/ConfigMap volumes —
mounted rw on the sidecar, ro on the agent). ``put_archive`` can't write a
read-only rootfs but it can write a mounted volume, so the sandbox files
land there before the sidecar starts. The one gap vs the K8s manifest: the
``local`` driver can't enforce a size cap, so the ``/tmp`` limit
``sizing.py`` computes is advisory here (recorded as a label).
"""

import asyncio
import contextlib
import io
import logging
import tarfile
import time
from typing import TYPE_CHECKING, Any

import aiodocker
import aiodocker.exceptions
from aiodocker.containers import DockerContainer

from api.plugins.container.backend import (
    ContainerBackend,
    ContainerRequest,
    ContainerResult,
    ContainerStatus,
)
from api.plugins.container.config import DockerConfig
from api.plugins.container.schema import (
    SANDBOX_PLACEHOLDER,
    SECURITY_PROXY_LOOPBACK,
    ContainerDockerStatus,
    DockerEventAction,
    ProxySpec,
)
from api.plugins.container.security_proxy import build_proxy_spec
from api.plugins.container.utils import (
    LABEL_EXECUTION_ID,
    LABEL_PLUGIN,
    LABEL_PROXY_CONTAINER,
    LABEL_ROLE,
    parse_memory_limit,
    parse_structured_logs,
)

if TYPE_CHECKING:
    from faststream.redis import RedisBroker
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Mount point for the per-execution sandbox volume (CA cert/key + proxy
# config). rw on the sidecar, ro on the agent. put_archive targets it
# because the container rootfs is read-only.
_SANDBOX_DIR = "/sandbox"
_AGENT_CA_PATH = f"{_SANDBOX_DIR}/ca.crt"

# How long to wait for the sidecar's mitmdump to bind 127.0.0.1:8080 before
# giving up on the launch. Its startup is a pip-free image doing a single
# CA concatenation, so this is generous.
_PROXY_READY_TIMEOUT_SECONDS = 30.0
_PROXY_READY_POLL_INTERVAL_SECONDS = 0.5

# Sidecar resource envelope — mirrors the K8s sidecar's requests/limits.
_PROXY_MEMORY_LIMIT = "256m"
_PROXY_CPU_LIMIT = 0.5


def _sanitize_container_name(value: str) -> str:
    """Make ``value`` a legal Docker container/volume name fragment.

    Docker allows ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``; execution ids are UUIDs so
    this only ever guards against an unexpected caller.
    """
    cleaned = "".join(c if (c.isalnum() or c in "_.-") else "-" for c in value)
    return cleaned.strip("-_.") or "x"


def _sandbox_tar(members: dict[str, bytes]) -> bytes:
    """Build a flat tar of ``members`` for ``put_archive`` into the sandbox volume.

    Files are 0444 — the sidecar copies the CA into its own writable
    confdir before mitmdump starts, and the agent only reads the cert.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class DockerBackend(ContainerBackend):
    """Docker backend for container execution and watching.

    Features:
    - Sandboxed two-container execution (agent + security-proxy sidecar)
    - Event-driven container monitoring (filtered by plugin label)
    - Idempotent status updates via Redis
    - Three-phase reconciliation for missed events and orphans
    - Bounded concurrency for event handling
    """

    MAX_CONCURRENT_HANDLERS = 100

    def __init__(
        self,
        plugin_name: str,
        config: DockerConfig,
        broker: RedisBroker,
        redis: Redis,
    ) -> None:
        """Initialize the Docker backend.

        Args:
            plugin_name: Plugin that owns this backend (e.g., "sentry").
            config: Docker configuration.
            broker: FastStream Redis broker for publishing.
            redis: Redis client for execution tracking.
        """
        super().__init__(plugin_name, broker, redis)
        self._config = config
        self._docker: aiodocker.Docker | None = None
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_HANDLERS)
        self._pending_tasks: set[asyncio.Task] = set()

    async def _get_docker(self) -> aiodocker.Docker:
        """Get or create Docker client."""
        if self._docker is None:
            url = self._config.socket
            if url.startswith("/"):
                url = f"unix://{url}"
            self._docker = aiodocker.Docker(url=url)
        return self._docker

    # === Sandbox resource naming ===

    def _agent_name(self, execution_id: str) -> str:
        return f"jc-{self._plugin_name}-{_sanitize_container_name(execution_id)}"

    def _proxy_name(self, execution_id: str) -> str:
        return f"{self._agent_name(execution_id)}-proxy"

    def _tmp_volume_name(self, execution_id: str) -> str:
        return f"{self._agent_name(execution_id)}-tmp"

    def _sandbox_volume_name(self, execution_id: str) -> str:
        return f"{self._agent_name(execution_id)}-sandbox"

    # === Container Execution ===

    async def start_container(self, execution_id: str, request: ContainerRequest) -> str:
        """Start a sandboxed agent + security-proxy pair and register it.

        The sidecar is created and started first; once its mitmdump is
        listening the agent is started sharing its network namespace. All
        resource names derive from ``execution_id`` and a retried execution
        keeps its id (ADR-010), so a previous attempt's leftovers are
        purged first.

        Returns:
            The agent container id.
        """
        docker = await self._get_docker()
        request.labels[LABEL_PLUGIN] = self._plugin_name

        proxy_spec = build_proxy_spec(
            secret_env=request.secrets,
            upstreams=request.upstreams,
            extra_hosts=request.extra_hosts,
            image=self._config.security_proxy_image,
            execution_id=execution_id,
            oauth_upstreams=request.oauth_upstreams,
        )

        agent_name = self._agent_name(execution_id)
        proxy_name = self._proxy_name(execution_id)
        tmp_volume = self._tmp_volume_name(execution_id)
        sandbox_volume = self._sandbox_volume_name(execution_id)

        await self._purge_stale_resources(execution_id)

        await self._ensure_image(proxy_spec.image)
        await self._ensure_image(request.image)

        vol_labels = {LABEL_PLUGIN: self._plugin_name, LABEL_EXECUTION_ID: execution_id}
        await docker.volumes.create({"Name": tmp_volume, "Driver": "local", "Labels": vol_labels})
        await docker.volumes.create(
            {"Name": sandbox_volume, "Driver": "local", "Labels": vol_labels}
        )

        proxy_container: DockerContainer | None = None
        try:
            proxy_container = await docker.containers.create(
                config=self._build_proxy_config(proxy_spec, sandbox_volume),
                name=proxy_name,
            )
            # put_archive can't write the container rootfs when it's
            # read-only, but it can write a mounted volume — so the CA and
            # config land in the sandbox volume (mounted rw here, ro on the
            # agent), not on either rootfs.
            await proxy_container.put_archive(
                _SANDBOX_DIR,
                _sandbox_tar(
                    {
                        "ca.crt": proxy_spec.ca_cert_pem.encode(),
                        "ca.key": proxy_spec.ca_key_pem.encode(),
                        "config.json": proxy_spec.config_json.encode(),
                    }
                ),
            )
            await proxy_container.start()
            await self._await_proxy_ready(proxy_container, execution_id)

            agent_container = await docker.containers.create(
                config=self._build_agent_config(
                    request, proxy_container.id, proxy_name, tmp_volume, sandbox_volume
                ),
                name=agent_name,
            )
            await agent_container.start()
        except Exception:
            logger.exception(
                "Sandbox launch failed for execution %s (plugin=%s) — cleaning up",
                execution_id,
                self._plugin_name,
            )
            await self._purge_stale_resources(execution_id)
            raise

        container_id = agent_container.id
        await self.register_execution(execution_id, container_id=container_id)

        logger.info(
            "Started sandboxed container %s (+ proxy %s) for execution %s (plugin=%s)",
            container_id[:12],
            proxy_container.id[:12],
            execution_id,
            self._plugin_name,
        )
        return container_id

    async def stop_container(self, container_id: str) -> None:
        """Stop a running agent container and its sidecar."""
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(container_id)
            info = await container.show()
            proxy_name = (info.get("Config", {}).get("Labels", {}) or {}).get(LABEL_PROXY_CONTAINER)
            await container.stop()
            logger.debug("Stopped container %s", container_id[:12])
            if proxy_name:
                await self._stop_container_by_name(proxy_name)
        except Exception as e:
            logger.warning("Failed to stop container %s: %s", container_id[:12], e)

    async def run(self, request: ContainerRequest) -> ContainerResult:
        """Run a container to completion with timeout, log collection, and cleanup.

        Unsandboxed direct execution — used only by the internal smoke
        workflow, which makes no external calls. Real dispatch always goes
        through :meth:`start_container`.
        """
        docker = await self._get_docker()
        await self._ensure_image(request.image)

        container = await docker.containers.create(
            config=self._build_container_config(request),
        )
        container_id: str = container.id
        start_time = time.monotonic()

        await container.start()
        logger.info("Started container %s (image=%s)", container_id[:12], request.image)

        timeout = request.timeout_seconds
        status = ContainerStatus.COMPLETED
        exit_code = -1
        try:
            try:
                result = await asyncio.wait_for(container.wait(), timeout=timeout)
                exit_code = result.get("StatusCode", -1)
                if exit_code != 0:
                    status = ContainerStatus.FAILED
            except TimeoutError:
                logger.warning(
                    "Container %s timed out after %ds, killing", container_id[:12], timeout
                )
                async with _suppress_docker_errors():
                    await container.kill()
                status = ContainerStatus.TIMEOUT
        finally:
            duration = time.monotonic() - start_time

            stdout = await self._collect_logs(container, stdout=True, stderr=False)
            stderr = await self._collect_logs(container, stdout=False, stderr=True)

            if self._config.cleanup_containers:
                async with _suppress_docker_errors():
                    await container.delete(force=True)
                logger.info("Cleaned up container %s", container_id[:12])

        return ContainerResult(
            container_id=container_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(duration, 2),
            status=status,
        )

    async def get_status(self, container_id: str) -> ContainerStatus:
        """Get the current status of a container."""
        docker = await self._get_docker()
        container = docker.containers.container(container_id)
        info = await container.show()
        state = info.get("State", {})

        if state.get("Running"):
            return ContainerStatus.RUNNING

        exit_code = state.get("ExitCode", -1)
        if state.get("OOMKilled") or exit_code != 0:
            return ContainerStatus.FAILED
        return ContainerStatus.COMPLETED

    async def get_logs(self, container_id: str) -> str:
        """Get combined stdout/stderr logs from a container."""
        docker = await self._get_docker()
        container = docker.containers.container(container_id)
        lines = await container.log(stdout=True, stderr=True, follow=False)
        return "".join(lines)

    async def health_check(self) -> bool:
        """Check Docker daemon connectivity."""
        try:
            docker = await self._get_docker()
            await docker.version()
            return True
        except Exception:
            return False

    # === Container Watching ===

    async def start_watching(self) -> None:
        """Start watching for container events filtered by plugin label."""
        docker = await self._get_docker()
        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop(docker))
        logger.info("Started container watcher for plugin=%s", self._plugin_name)

    async def stop_watching(self) -> None:
        """Stop watching for container events."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

        # Wait for pending event handlers
        if self._pending_tasks:
            logger.info(f"Waiting for {len(self._pending_tasks)} pending event handlers...")
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        if self._docker:
            await self._docker.close()
            self._docker = None

        logger.info("Stopped container watcher for plugin=%s", self._plugin_name)

    async def _watch_loop(self, docker: aiodocker.Docker) -> None:
        """Main watch loop for Docker events.

        Subscribes to container events filtered by both the execution_id
        label (must exist) and the plugin label (must match this backend's
        plugin). The sidecar carries neither the execution_id label nor a
        cli role, so it never trips this filter — only agent containers do.
        """
        while self._running:
            try:
                subscriber = docker.events.subscribe(
                    filters={
                        "type": ["container"],
                        "label": [
                            LABEL_EXECUTION_ID,
                            f"{LABEL_PLUGIN}={self._plugin_name}",
                        ],
                    }
                )
                while self._running:
                    event = await subscriber.get()
                    if event is None:
                        break
                    task = asyncio.create_task(self._handle_event_with_limit(event))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Docker event watch loop: {e}")
                if self._running:
                    await asyncio.sleep(5)

    async def _handle_event_with_limit(self, event: dict[str, Any]) -> None:
        """Handle container event with concurrency limiting."""
        async with self._semaphore:
            await self._handle_container_event(event)

    async def _handle_container_event(self, event: dict[str, Any]) -> None:
        """Handle a Docker container event."""
        action = event.get("Action", "")
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})
        container_id = actor.get("ID", "")

        execution_id = attributes.get(LABEL_EXECUTION_ID)
        if not execution_id:
            return

        logger.debug(
            "Container event: %s for execution %s (plugin=%s)",
            action,
            execution_id,
            self._plugin_name,
        )

        if action == DockerEventAction.DIE:
            await self._handle_container_exit(
                execution_id=execution_id,
                container_id=container_id,
                exit_code=int(attributes.get("exitCode", 1)),
            )

    async def _handle_container_exit(
        self,
        execution_id: str,
        container_id: str,
        exit_code: int,
    ) -> None:
        """Handle container exit event with idempotency.

        Uses publish_status_idempotent to prevent duplicate processing.
        """
        result, error_info, rate_limit_events = await self._get_container_logs_result(container_id)
        status, final_error_info, retry_at = await self.resolve_exit_outcome(
            execution_id, exit_code, error_info, rate_limit_events
        )

        published = await self.publish_status_idempotent(
            execution_id=execution_id,
            status=status,
            container_id=container_id,
            exit_code=exit_code,
            error_type=final_error_info[0] if final_error_info else None,
            error_message=final_error_info[1] if final_error_info else None,
            result=result,
            retry_at=retry_at,
        )

        # Only cleanup if we actually processed this exit
        if published:
            await self._cleanup_execution(execution_id)

    async def _get_container_logs_result(
        self,
        container_id: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None, list[dict[str, Any]]]:
        """Get and parse container logs for result/error."""
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(container_id)
            logs = await container.log(stdout=True, stderr=True)
            log_text = "".join(logs)
            return parse_structured_logs(log_text)
        except Exception as e:
            logger.error(f"Failed to parse container logs: {e}")
            return None, None, []

    async def _cleanup_execution(self, execution_id: str) -> None:
        """Remove the agent container, its sidecar, and both volumes.

        Every name derives from ``execution_id``, so this works whether or
        not the agent is still inspectable (it isn't, on the reconcile
        orphan path). The agent is removed first — the sidecar owns the
        shared network namespace, so tearing it down first would kill a
        still-running agent.
        """
        if not self._config.cleanup_containers:
            # The agent already exited on its own (that's what triggered this call),
            # but mitmdump never exits by itself — without an explicit stop, a
            # completed execution leaks a live, network-reachable proxy forever.
            logger.debug("Cleanup disabled for execution %s; stopping sidecar only", execution_id)
            await self._stop_container_by_name(self._proxy_name(execution_id))
            return

        await self._remove_container_by_name(self._agent_name(execution_id))
        await self._remove_container_by_name(self._proxy_name(execution_id))
        await self._remove_volume(self._tmp_volume_name(execution_id))
        await self._remove_volume(self._sandbox_volume_name(execution_id))

    # === Reconciliation ===

    async def reconcile(self) -> None:
        """Reconcile container state with Redis tracking and database.

        Three-phase reconciliation scoped to this plugin's containers:
        1. Exited containers whose events were missed (force-publishes)
        2. Orphaned executions (in Redis but container gone)
        3. Stale DB executions (queued/processing in DB but no container running)

        Protected by a distributed lock so only one instance runs at a time.
        """
        docker = await self._get_docker()

        try:
            # Get containers and active executions in parallel
            containers = await docker.containers.list(
                all=True,
                filters={
                    "label": [
                        LABEL_EXECUTION_ID,
                        f"{LABEL_PLUGIN}={self._plugin_name}",
                    ]
                },
            )
            active_executions = await self.get_active_executions()

            # Build mapping of execution_id -> container info
            container_map: dict[str, tuple[DockerContainer, dict]] = {}
            for container in containers:
                info = await container.show()
                labels = info.get("Config", {}).get("Labels", {})
                exec_id = labels.get(LABEL_EXECUTION_ID)
                if exec_id:
                    container_map[exec_id] = (container, info)

            # Phase 1: Process exited containers still in the active set.
            # These are containers whose DIE event was missed (e.g., after
            # a backend restart). Only process if still tracked as active —
            # otherwise the DIE handler already published the status.
            for exec_id, (_container, info) in container_map.items():
                state = info.get("State", {})
                if (
                    state.get("Status") == ContainerDockerStatus.EXITED
                    and exec_id in active_executions
                ):
                    container_id = info.get("Id", "")
                    exit_code = state.get("ExitCode", 1)

                    logger.info(
                        "Reconciling exited container %s for execution %s (plugin=%s)",
                        container_id[:12],
                        exec_id,
                        self._plugin_name,
                    )
                    await self._reconcile_container_exit(
                        execution_id=exec_id,
                        container_id=container_id,
                        exit_code=exit_code,
                    )

            # Phase 2: Handle orphaned executions (in Redis but no container)
            container_exec_ids = set(container_map.keys())
            orphaned = active_executions - container_exec_ids

            for exec_id in orphaned:
                logger.warning(
                    f"Found orphaned execution {exec_id} (plugin={self._plugin_name}) "
                    f"- container not found"
                )
                await self.unregister_execution(exec_id)
                await self.publish_status(
                    execution_id=exec_id,
                    status="failed",
                    error_type="container_error",
                    error_message="Container not found - may have been removed unexpectedly",
                )
                # The agent is gone but its sidecar / volumes may have
                # outlived it (missed DIE, backend crash mid-cleanup).
                await self._remove_container_by_name(self._proxy_name(exec_id))
                await self._remove_volume(self._tmp_volume_name(exec_id))
                await self._remove_volume(self._sandbox_volume_name(exec_id))

            # Phase 3: Fail stale DB executions whose container no longer exists.
            # Scoping to this plugin's executions happens inside
            # ``fail_stale_db_executions`` via ``Execution.provider`` (#104).
            running_container_ids = {
                exec_id
                for exec_id, (_c, info) in container_map.items()
                if info.get("State", {}).get("Status")
                in (ContainerDockerStatus.RUNNING, ContainerDockerStatus.CREATED)
            }
            await self.fail_stale_db_executions(running_container_ids)

        except Exception as e:
            logger.error(f"Error during reconciliation (plugin={self._plugin_name}): {e}")

    async def _reconcile_container_exit(
        self,
        execution_id: str,
        container_id: str,
        exit_code: int,
    ) -> None:
        """Handle container exit during reconciliation.

        Unlike _handle_container_exit, this bypasses the idempotency check
        and always publishes status. Safe because reconciliation is protected
        by a distributed lock.
        """
        result, error_info, rate_limit_events = await self._get_container_logs_result(container_id)
        status, final_error_info, retry_at = await self.resolve_exit_outcome(
            execution_id, exit_code, error_info, rate_limit_events
        )

        # Remove from active set (may already be gone — that's fine)
        await self.unregister_execution(execution_id)

        # Always publish — this is the safety net for lost pub/sub messages
        await self.publish_status(
            execution_id=execution_id,
            status=status,
            container_id=container_id,
            exit_code=exit_code,
            error_type=final_error_info[0] if final_error_info else None,
            error_message=final_error_info[1] if final_error_info else None,
            result=result,
            retry_at=retry_at,
        )

        await self._cleanup_execution(execution_id)

    # === Internal Helpers ===

    def _build_container_config(self, request: ContainerRequest) -> dict:
        """Build the Docker container config for an unsandboxed :meth:`run`."""
        host_config: dict = {
            "Memory": parse_memory_limit(self._config.memory_limit),
            "NanoCpus": int(self._config.cpu_limit * 1e9),
        }
        if self._config.network:
            host_config["NetworkMode"] = self._config.network

        config: dict = {
            "Image": request.image,
            "Env": [f"{k}={v}" for k, v in request.env.items()],
            "Labels": request.labels,
            "HostConfig": host_config,
        }
        if request.command:
            config["Cmd"] = request.command

        return config

    def _build_proxy_config(self, proxy: ProxySpec, sandbox_volume: str) -> dict:
        """Build the security-proxy sidecar container config.

        Read-only rootfs, all capabilities dropped, no privilege
        escalation, non-root (baked into the image). A tiny tmpfs covers
        the confdir the entrypoint writes ``mitmproxy-ca.pem`` into; the
        sandbox volume (rw here) carries the CA + config. The sidecar is
        the container that joins ``self._config.network``; the agent rides
        its namespace.
        """
        env = dict(proxy.secret_env)
        env["SECURITY_PROXY_CA_DIR"] = _SANDBOX_DIR
        env["SECURITY_PROXY_CONFIG"] = f"{_SANDBOX_DIR}/config.json"

        host_config: dict = {
            "Memory": parse_memory_limit(_PROXY_MEMORY_LIMIT),
            "NanoCpus": int(_PROXY_CPU_LIMIT * 1e9),
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,mode=1777,size=16m"},
            "Binds": [f"{sandbox_volume}:{_SANDBOX_DIR}"],
        }
        if self._config.network:
            host_config["NetworkMode"] = self._config.network

        return {
            "Image": proxy.image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            # No execution_id label: keeps the sidecar out of the watcher's
            # event filter (which requires that label to be present).
            "Labels": {
                LABEL_PLUGIN: self._plugin_name,
                LABEL_ROLE: "security-proxy",
            },
            "HostConfig": host_config,
            "Healthcheck": {
                "Test": [
                    "CMD",
                    "python3",
                    "-c",
                    "import socket; s=socket.socket(); s.settimeout(1); "
                    "s.connect(('127.0.0.1', 8080)); s.close()",
                ],
                "Interval": 1_000_000_000,
                "Timeout": 2_000_000_000,
                "Retries": 30,
                "StartPeriod": 1_000_000_000,
            },
        }

    def _build_agent_config(
        self,
        request: ContainerRequest,
        proxy_container_id: str,
        proxy_name: str,
        tmp_volume: str,
        sandbox_volume: str,
    ) -> dict:
        """Build the agent container config — no credentials, egress via proxy."""
        env = dict(request.env)
        env.update(
            {
                "HTTPS_PROXY": SECURITY_PROXY_LOOPBACK,
                "HTTP_PROXY": SECURITY_PROXY_LOOPBACK,
                "NO_PROXY": "127.0.0.1,localhost",
                "NODE_EXTRA_CA_CERTS": _AGENT_CA_PATH,
                "SSL_CERT_FILE": _AGENT_CA_PATH,
                "REQUESTS_CA_BUNDLE": _AGENT_CA_PATH,
                "GIT_SSL_CAINFO": _AGENT_CA_PATH,
                "CURL_CA_BUNDLE": _AGENT_CA_PATH,
            }
        )
        # Seed every sidecar-owned credential with a placeholder so tools
        # that hard-require their auth env var at startup are satisfied.
        for key in request.secrets:
            env[key] = SANDBOX_PLACEHOLDER

        host_config: dict = {
            "Memory": parse_memory_limit(self._config.memory_limit),
            "NanoCpus": int(self._config.cpu_limit * 1e9),
            # Share the sidecar's netns: 127.0.0.1:8080 reaches mitmdump and
            # nothing else can. Mirrors a K8s pod's shared loopback.
            "NetworkMode": f"container:{proxy_container_id}",
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges"],
            "Binds": [f"{tmp_volume}:/tmp", f"{sandbox_volume}:{_SANDBOX_DIR}:ro"],
        }

        labels = {**request.labels, LABEL_PROXY_CONTAINER: proxy_name}
        # Advisory only — the local volume driver can't cap size (see
        # module docstring); recorded so it's visible on inspect.
        if request.tmp_size_limit:
            labels["jeanclode.tmp_size"] = request.tmp_size_limit

        config: dict = {
            "Image": request.image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": labels,
            "HostConfig": host_config,
        }
        if request.command:
            config["Cmd"] = request.command
        return config

    async def _await_proxy_ready(self, proxy_container: DockerContainer, execution_id: str) -> None:
        """Block until the sidecar's healthcheck reports it is listening."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PROXY_READY_TIMEOUT_SECONDS
        while loop.time() < deadline:
            info = await proxy_container.show()
            state = info.get("State", {})
            health = (state.get("Health") or {}).get("Status")
            if health == "healthy":
                return
            if not state.get("Running") and state.get("ExitCode", 0) != 0:
                logs = await self._collect_logs(proxy_container, stdout=True, stderr=True)
                raise RuntimeError(
                    f"security-proxy for execution {execution_id} exited before ready: "
                    f"{logs[-2000:]}"
                )
            await asyncio.sleep(_PROXY_READY_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"security-proxy for execution {execution_id} not ready after "
            f"{_PROXY_READY_TIMEOUT_SECONDS:.0f}s"
        )

    async def _purge_stale_resources(self, execution_id: str) -> None:
        """Remove anything a previous attempt left under this execution's names.

        A redispatched execution (ADR-010) keeps its id, so the agent
        container, the sidecar and both volumes all collide with the first
        attempt. Best-effort — a failed delete surfaces as the create's own
        409 later.
        """
        await self._remove_container_by_name(self._agent_name(execution_id))
        await self._remove_container_by_name(self._proxy_name(execution_id))
        await self._remove_volume(self._tmp_volume_name(execution_id))
        await self._remove_volume(self._sandbox_volume_name(execution_id))

    async def _remove_container_by_name(self, name: str) -> None:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(name)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.warning("Could not look up container %s: %s", name, e)
            return
        try:
            await container.delete(force=True)
            logger.debug("Removed container %s", name)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.warning("Failed to remove container %s: %s", name, e)

    async def _stop_container_by_name(self, name: str) -> None:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(name)
            await container.stop()
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.warning("Failed to stop container %s: %s", name, e)

    async def _remove_volume(self, name: str) -> None:
        docker = await self._get_docker()
        try:
            volume = await docker.volumes.get(name)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.warning("Could not look up volume %s: %s", name, e)
            return
        try:
            await volume.delete(force=True)
            logger.debug("Removed volume %s", name)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.warning("Failed to remove volume %s: %s", name, e)

    async def _ensure_image(self, image: str) -> None:
        """Pull image if not present locally."""
        docker = await self._get_docker()
        try:
            await docker.images.inspect(image)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                raise
            logger.info("Pulling image %s", image)
            await docker.images.pull(image)

    async def _collect_logs(
        self, container: aiodocker.DockerContainer, *, stdout: bool, stderr: bool
    ) -> str:
        """Collect logs from a container."""
        try:
            lines = await container.log(stdout=stdout, stderr=stderr, follow=False)
            return "".join(lines)
        except Exception:
            logger.warning("Failed to collect logs from container %s", container.id[:12])
            return ""


class _suppress_docker_errors:
    """Context manager that suppresses Docker API errors."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and issubclass(exc_type, aiodocker.exceptions.DockerError):
            logger.debug("Suppressed Docker error: %s", exc_val)
            return True
        return False
