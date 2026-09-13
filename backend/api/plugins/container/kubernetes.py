"""Kubernetes backend for container execution and watching."""

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import kr8s.asyncio
from kr8s import NotFoundError
from kr8s.asyncio.objects import ConfigMap, Job, Pod, Secret
from pydantic import BaseModel

from api.plugins.container.backend import ContainerBackend, ContainerRequest
from api.plugins.container.config import KubernetesConfig
from api.plugins.container.schema import (
    CONTAINER_NAME,
    SANDBOX_PLACEHOLDER,
    SECURITY_PROXY_CA_DIR_SIDECAR,
    SECURITY_PROXY_CA_PATH_AGENT,
    SECURITY_PROXY_CONFIG_DIR,
    SECURITY_PROXY_CONTAINER_NAME,
    SECURITY_PROXY_LOOPBACK,
    ConditionStatus,
    JobConditionType,
    ProxySpec,
    WatchEventType,
)
from api.plugins.container.security_proxy import build_proxy_spec
from api.plugins.container.utils import (
    LABEL_EXECUTION_ID,
    LABEL_PLUGIN,
    parse_structured_logs,
)

if TYPE_CHECKING:
    from faststream.redis import RedisBroker
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# K8s label values: max 63 chars, alphanumeric start/end, [a-zA-Z0-9._-] inside.
_LABEL_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]")

# TTL in seconds for finished Jobs when cleanup_jobs is enabled.
_CLEANUP_TTL_SECONDS = 600

# How many trailing log lines to pull when parsing a Job's result markers.
# The CLI emits [JEANCLODE:RESULT]/[JEANCLODE:ERROR] just before exit; 2000
# lines comfortably covers trailing stack traces / shutdown output without
# transferring the whole (potentially tens-of-MB) agent log.
_LOG_TAIL_LINES = 2000

# Timeout in seconds when draining pending handlers during shutdown.
_DRAIN_TIMEOUT_SECONDS = 120.0

# How long to wait for a superseded Job to finish disappearing before we
# reuse its name anyway. Foreground deletion of an already-terminal Job is
# near-instant; the headroom is for one whose pod is still terminating
# (default 30s grace period).
_PURGE_TIMEOUT_SECONDS = 60.0
_PURGE_POLL_INTERVAL_SECONDS = 0.5

# How long the "which Job generation is this execution on" marker outlives
# its launch. Only needs to cover a single run; the value is generous so a
# long-running Job's DELETED event is still attributable.
_JOB_UID_TTL_SECONDS = 86400

# /tmp sizeLimit used only if a dispatch path doesn't compute one via
# api.plugins.container.sizing (see ContainerRequest.tmp_size_limit) —
# every current launch path does, so this is a defensive fallback, not a
# tuning knob.
_FALLBACK_TMP_SIZE_LIMIT = "1Gi"

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _endpoint(url: str) -> tuple[str, int] | None:
    """``(host, port)`` for ``url``, with the scheme's default port applied.

    kr8s builds the in-cluster server as ``https://<ip>:443`` while httpx
    logs the same host with the default port dropped, so the raw netlocs
    never compare equal — normalize both ends before matching.
    """
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    resolved = port or _DEFAULT_PORTS.get(parts.scheme)
    return None if resolved is None else (host, resolved)


class _ApiServerRequestLogFilter(logging.Filter):
    """Drop httpx's per-request INFO line for calls to the API server.

    The Job watchers poll several times a second and httpx logs every
    request under its own logger, so matching the API server's endpoint is
    the only way to mute that noise without muting every other httpx call
    the backend makes.
    """

    def __init__(self, endpoint: tuple[str, int]) -> None:
        super().__init__()
        self._endpoint = endpoint

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 2:
            return True
        return _endpoint(str(args[1])) != self._endpoint


_silenced_api_servers: set[tuple[str, int]] = set()


def _silence_api_request_logs(server: object) -> None:
    """Mute httpx request logs for one API server, once per process.

    Takes ``object`` rather than ``str`` so a client that never resolved a
    server URL degrades to noisier logs instead of a failed dispatch.
    """
    if not isinstance(server, str):
        return
    endpoint = _endpoint(server)
    if endpoint is None or endpoint in _silenced_api_servers:
        return
    _silenced_api_servers.add(endpoint)
    logging.getLogger("httpx").addFilter(_ApiServerRequestLogFilter(endpoint))


def _sanitize_label_value(value: str) -> str:
    """Sanitize a string to be a valid Kubernetes label value."""
    value = _LABEL_UNSAFE.sub("-", value)[:63]
    return value.strip("-_.")


def _job_uid(job_obj: Any) -> str | None:
    """Best-effort ``metadata.uid`` of a Job.

    A Job name is reused across retries of one execution but its UID is
    not, which is what lets a DELETED event be attributed to the right
    generation. Returns ``None`` when the field isn't there, in which case
    callers fall back to their unguarded behaviour.
    """
    metadata = getattr(job_obj, "metadata", None)
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    return uid if isinstance(uid, str) and uid else None


# Reserve enough headroom on the Job-derived prefix to fit the longest child
# resource suffix below comfortably under K8s' 253-char DNS subdomain limit.
_RESOURCE_PREFIX_MAX = 50


class _ResourceNames(BaseModel):
    """Names for the child resources owned by a single Job.

    Every Job has the full set: an agent secret (only created if the agent
    has any non-credential env vars), and the three sandbox resources
    (proxy secret, CA secret, proxy ConfigMap) — sandbox is unconditional.
    """

    agent_secret: str
    proxy_secret: str
    ca_secret: str
    proxy_config: str

    @classmethod
    def derive(cls, job_name: str) -> _ResourceNames:
        base = job_name[:_RESOURCE_PREFIX_MAX]
        return cls(
            agent_secret=f"{base}-secrets",
            proxy_secret=f"{base}-pxs",
            ca_secret=f"{base}-pxca",
            proxy_config=f"{base}-pxc",
        )

    def children(self) -> list[tuple[Any, str]]:
        """Every child resource name, paired with its kr8s class.

        Used to sweep leftovers before reusing a Job name. ``agent_secret``
        is listed unconditionally even though it isn't always created —
        the sweep tolerates a missing resource, and a previous attempt may
        well have had public env when this one doesn't.
        """
        return [
            (Secret, self.agent_secret),
            (Secret, self.proxy_secret),
            (Secret, self.ca_secret),
            (ConfigMap, self.proxy_config),
        ]


def _sandbox_volumes(names: _ResourceNames) -> list[dict[str, Any]]:
    """Volumes added to the pod when sandbox mode is enabled.

    The CA Secret is mounted twice: once read-only into the sidecar with
    cert + key, and a second projection (cert only) into the agent's trust
    store via ``items``. The agent never sees the CA's private key.
    """
    return [
        {
            "name": "security-proxy-config",
            "configMap": {"name": names.proxy_config},
        },
        {
            "name": "security-proxy-ca-priv",
            "secret": {"secretName": names.ca_secret},
        },
        {
            "name": "security-proxy-ca",
            "secret": {
                "secretName": names.ca_secret,
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        },
        # Sidecar-only /tmp so the agent can't read the assembled CA pem.
        {"name": "security-proxy-tmp", "emptyDir": {"sizeLimit": "16Mi"}},
    ]


class KubernetesBackend(ContainerBackend):
    """Kubernetes backend for container execution and watching.

    Features:
    - Event-driven Job monitoring via Kubernetes Watch API
    - Ephemeral Secrets for env vars (auto-cleanup via ownerReference)
    - Idempotent status updates via Redis
    - Three-phase reconciliation for missed events and orphans
    - Bounded concurrency for event handling
    """

    MAX_CONCURRENT_HANDLERS = 100

    def __init__(
        self,
        plugin_name: str,
        config: KubernetesConfig,
        broker: RedisBroker,
        redis: Redis,
    ) -> None:
        super().__init__(plugin_name, broker, redis)
        self._config = config
        self._api: kr8s.asyncio.Api | None = None
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_HANDLERS)
        self._pending_tasks: set[asyncio.Task] = set()

    async def _get_api(self) -> kr8s.asyncio.Api:
        """Get or create the kr8s async API client.

        kr8s auto-discovers credentials (in-cluster ServiceAccount, kubeconfig).
        """
        if self._api is None:
            self._api = await kr8s.asyncio.api()
            _silence_api_request_logs(self._api.auth.server)
        return self._api

    # === Container Execution ===

    async def start_container(self, execution_id: str, request: ContainerRequest) -> str:
        """Start a sandboxed Kubernetes Job and register it as active.

        Every Job is sandboxed — there is no opt-out. ``request.env`` is
        partitioned: known credential keys are routed to the security-proxy
        sidecar; the remainder stays on the agent. A short-lived CA is
        minted per execution. All child resources (Secret(s), ConfigMap)
        are tied to the Job via ownerReference so they cascade-delete.

        Every resource name here derives from ``execution_id``, and a
        retried execution keeps its id (ADR-010), so anything the previous
        attempt left behind is purged first — see
        :meth:`_purge_stale_resources`.

        Returns:
            Job name as the container_id.
        """
        api = await self._get_api()

        request.labels[LABEL_PLUGIN] = self._plugin_name

        proxy = build_proxy_spec(
            secret_env=request.secrets,
            upstreams=request.upstreams,
            extra_hosts=request.extra_hosts,
            image=self._config.security_proxy_image,
            execution_id=execution_id,
            oauth_upstreams=request.oauth_upstreams,
        )
        public_env = request.env

        job_name = f"jc-{self._plugin_name}-{_sanitize_label_value(execution_id)}"[:63]
        names = _ResourceNames.derive(job_name)

        await self._purge_stale_resources(job_name, names, api)

        creates = [
            self._create_secret(names.proxy_secret, proxy.secret_env, api),
            self._create_secret(
                names.ca_secret,
                {"ca.crt": proxy.ca_cert_pem, "ca.key": proxy.ca_key_pem},
                api,
            ),
            self._create_configmap(names.proxy_config, {"config.json": proxy.config_json}, api),
        ]
        if public_env:
            creates.append(self._create_secret(names.agent_secret, public_env, api))
        await asyncio.gather(*creates)

        job_manifest = self._build_job_manifest(
            job_name, names, request, public_env=public_env, proxy=proxy
        )
        job = Job(job_manifest, api=api)
        await job.create()
        await self._record_job_uid(execution_id, _job_uid(job))

        adopts = [
            self._adopt(Secret, names.proxy_secret, job, api),
            self._adopt(Secret, names.ca_secret, job, api),
            self._adopt(ConfigMap, names.proxy_config, job, api),
        ]
        if public_env:
            adopts.append(self._adopt(Secret, names.agent_secret, job, api))
        await asyncio.gather(*adopts)

        await self.register_execution(execution_id, container_id=job_name)

        logger.info(
            "Created sandboxed Job %s for execution %s (plugin=%s, namespace=%s)",
            job_name,
            execution_id,
            self._plugin_name,
            self._config.namespace,
        )
        return job_name

    def _job_uid_key(self, execution_id: str) -> str:
        """Redis key holding the UID of the Job this execution last launched."""
        return f"jeanclode:{self._plugin_name}:job-uid:{execution_id}"

    async def _record_job_uid(self, execution_id: str, job_uid: str | None) -> None:
        """Remember which Job generation this execution is currently on."""
        if not job_uid:
            return
        try:
            await self._redis.set(self._job_uid_key(execution_id), job_uid, ex=_JOB_UID_TTL_SECONDS)
        except Exception as e:
            logger.warning("Failed to record Job UID for execution %s: %s", execution_id, e)

    async def _is_current_job_uid(self, execution_id: str, job_uid: str) -> bool:
        """Whether ``job_uid`` is the generation this execution last launched.

        An unknown answer — never recorded, expired, or Redis unreachable —
        counts as current: the guard only ever suppresses an event it can
        positively attribute to a superseded generation.
        """
        try:
            recorded = await self._redis.get(self._job_uid_key(execution_id))
        except Exception as e:
            logger.debug("Failed to read Job UID for execution %s: %s", execution_id, e)
            return True
        if recorded is None:
            return True
        if isinstance(recorded, bytes):
            recorded = recorded.decode()
        return bool(recorded == job_uid)

    async def _purge_stale_resources(
        self,
        job_name: str,
        names: _ResourceNames,
        api: kr8s.asyncio.Api,
    ) -> None:
        """Clear anything a previous attempt left under these names.

        Job and child names are derived from the execution id, and a
        redispatched execution (ADR-010 rate-limit retry) keeps its id — so
        the second attempt collides with the first. With ``cleanup_jobs``
        disabled the old Job is simply still there; even with it enabled,
        its Secrets/ConfigMap are reaped asynchronously by the garbage
        collector and can briefly outlive it. Either way the creates below
        fail with 409 Conflict and the retry never launches.

        The Job goes first, with Foreground propagation: the API server
        keeps the object (holding a finalizer) until its pods and owned
        children are gone, so waiting for it to disappear is what makes the
        whole name family safe to reuse. Only orphans — children whose
        owner was already gone, or that were never adopted — survive that,
        and they get swept by name afterwards.

        Best-effort throughout: if a delete fails we log and let the create
        surface the conflict, exactly as it does today.
        """
        await self._delete_job_and_wait(job_name, api)
        await asyncio.gather(
            *(self._delete_if_exists(kind, name, api) for kind, name in names.children())
        )

    async def _delete_job_and_wait(self, job_name: str, api: kr8s.asyncio.Api) -> None:
        """Foreground-delete ``job_name`` if present, then wait for it to go."""
        try:
            job = await Job.get(job_name, namespace=self._config.namespace, api=api)
        except NotFoundError:
            return
        except Exception as e:
            logger.warning("Could not look up Job %s before reusing its name: %s", job_name, e)
            return

        logger.info("Deleting superseded Job %s before reusing its name", job_name)
        try:
            await job.delete(propagation_policy="Foreground")
        except NotFoundError:
            return
        except Exception as e:
            logger.warning("Failed to delete superseded Job %s: %s", job_name, e)
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PURGE_TIMEOUT_SECONDS
        while loop.time() < deadline:
            try:
                await Job.get(job_name, namespace=self._config.namespace, api=api)
            except NotFoundError:
                return
            except Exception as e:
                # Transient API error — keep waiting rather than racing ahead.
                logger.debug("Error polling superseded Job %s: %s", job_name, e)
            await asyncio.sleep(_PURGE_POLL_INTERVAL_SECONDS)

        logger.warning(
            "Superseded Job %s still present after %.0fs — proceeding anyway",
            job_name,
            _PURGE_TIMEOUT_SECONDS,
        )

    async def _delete_if_exists(
        self,
        kind: Any,
        name: str,
        api: kr8s.asyncio.Api,
    ) -> None:
        """Delete a leftover child resource by name, tolerating its absence.

        ``kind`` is a kr8s resource class (Secret or ConfigMap) — typed as
        ``Any`` because kr8s' classmethod ``.get`` isn't well-typed.
        """
        try:
            obj = await kind.get(name, namespace=self._config.namespace, api=api)
        except NotFoundError:
            return
        except Exception as e:
            logger.warning(
                "Could not look up %s %s before reusing its name: %s", kind.kind, name, e
            )
            return

        try:
            await obj.delete()
            logger.info("Deleted orphaned %s %s left by a previous attempt", kind.kind, name)
        except NotFoundError:
            return
        except Exception as e:
            logger.warning("Failed to delete orphaned %s %s: %s", kind.kind, name, e)

    async def _create_secret(
        self,
        name: str,
        data: dict[str, str],
        api: kr8s.asyncio.Api,
    ) -> None:
        """Create an ephemeral Opaque Secret with stringData."""
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": self._config.namespace},
            "type": "Opaque",
            "stringData": data,
        }
        await Secret(manifest, api=api).create()
        logger.debug("Created Secret %s", name)

    async def _create_configmap(
        self,
        name: str,
        data: dict[str, str],
        api: kr8s.asyncio.Api,
    ) -> None:
        """Create an ephemeral ConfigMap."""
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self._config.namespace},
            "data": data,
        }
        await ConfigMap(manifest, api=api).create()
        logger.debug("Created ConfigMap %s", name)

    async def _adopt(
        self,
        kind: Any,
        name: str,
        job_obj: Job,
        api: kr8s.asyncio.Api,
    ) -> None:
        """Set ownerReference on a child resource so it deletes with the Job.

        ``kind`` is a kr8s resource class (Secret or ConfigMap) — typed as
        ``Any`` because kr8s' classmethod ``.get`` isn't well-typed.
        """
        obj = await kind.get(name, namespace=self._config.namespace, api=api)
        owner_reference = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_obj.name,
            "uid": job_obj.metadata.uid,
            "blockOwnerDeletion": True,
        }
        await obj.patch({"metadata": {"ownerReferences": [owner_reference]}})

    async def stop_container(self, container_id: str) -> None:
        """Delete a Kubernetes Job with background propagation."""
        try:
            api = await self._get_api()
            job = await Job.get(container_id, namespace=self._config.namespace, api=api)
            await job.delete(propagation_policy="Background")
            logger.debug("Deleted Job %s", container_id)
        except Exception as e:
            logger.warning("Failed to delete Job %s: %s", container_id, e)

    # === Watching ===

    async def start_watching(self) -> None:
        """Start watching for Kubernetes Job events."""
        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(
            "Started Kubernetes watcher for plugin=%s (namespace=%s)",
            self._plugin_name,
            self._config.namespace,
        )

    async def stop_watching(self) -> None:
        """Stop the watch loop and drain pending handlers."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

        self._api = None
        logger.info(
            "Stopped Kubernetes watcher for plugin=%s",
            self._plugin_name,
        )

    async def _watch_loop(self) -> None:
        """Main watch loop for Kubernetes Job events.

        Single long-lived stream — ``kr8s.asyncio.watch`` is an async
        iterator that runs until cancelled or until the upstream
        connection errors, handling 410 Gone and reconnection internally.
        The outer ``while`` is purely defensive: re-establish on
        unexpected exit, with backoff.
        """
        label_selector = {
            LABEL_PLUGIN: self._plugin_name,
        }

        # Process Jobs that already reached a terminal state before we
        # started watching (e.g. handler crashed mid-flight and was
        # restarted). The watch itself picks up everything after.
        await self._replay_terminal_jobs(label_selector)

        while self._running:
            try:
                api = await self._get_api()
                async for event_type, job_obj in kr8s.asyncio.watch(
                    "jobs",
                    namespace=self._config.namespace,
                    label_selector=label_selector,
                    api=api,
                ):
                    if not self._running:
                        break
                    task = asyncio.create_task(self._handle_event_with_limit(event_type, job_obj))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in Kubernetes Job watch loop: %s", e)
                if self._running:
                    await asyncio.sleep(5)

        # Drain pending handlers on exit
        if self._pending_tasks:
            logger.info("Waiting for %d pending event handlers...", len(self._pending_tasks))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending_tasks, return_exceptions=True),
                    timeout=_DRAIN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for %d pending handlers",
                    len(self._pending_tasks),
                )

    async def _replay_terminal_jobs(self, label_selector: dict[str, str]) -> None:
        """LIST Jobs once at startup, handle any already in a terminal state.

        Covers the gap where the watcher restarted while Jobs were
        completing. Live events are picked up by the watch itself.
        """
        try:
            api = await self._get_api()
            async for job_obj in Job.list(
                namespace=self._config.namespace,
                label_selector=label_selector,
                api=api,
            ):
                exit_code = self._get_job_terminal_state(job_obj)
                if exit_code is None:
                    continue
                execution_id = (job_obj.labels or {}).get(LABEL_EXECUTION_ID)
                if execution_id:
                    task = asyncio.create_task(
                        self._handle_event_with_limit(WatchEventType.MODIFIED, job_obj)
                    )
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
        except Exception as e:
            logger.warning("Failed to replay terminal Jobs: %s", e)

    async def _handle_event_with_limit(self, event_type: str, job_obj: Any) -> None:
        """Handle Job event with concurrency limiting."""
        async with self._semaphore:
            await self._handle_job_event(event_type, job_obj)

    def _get_job_terminal_state(self, job_obj: Any) -> int | None:
        """Check if Job is in terminal state.

        Returns:
            Exit code (0=success, 1=failed) or None if still running.
        """
        conditions = (job_obj.status or {}).get("conditions", []) if job_obj.status else []
        for condition in conditions:
            if condition.get("status") != ConditionStatus.TRUE:
                continue
            if condition.get("type") == JobConditionType.COMPLETE:
                return 0
            if condition.get("type") == JobConditionType.FAILED:
                return 1
        return None

    async def _handle_job_event(self, event_type: str, job_obj: Any) -> None:
        """Handle a Kubernetes Job event.

        Dispatches to the appropriate handler based on event type:
        - DELETED: marks execution as failed if still tracked
        - ADDED/MODIFIED: checks for terminal state and processes exit
        """
        labels = job_obj.labels or {}
        execution_id = labels.get(LABEL_EXECUTION_ID)
        if not execution_id:
            return

        job_name = job_obj.name

        logger.debug(
            "Job event: %s for execution %s (plugin=%s)",
            event_type,
            execution_id,
            self._plugin_name,
        )

        match event_type:
            case WatchEventType.DELETED:
                await self._handle_job_deleted(execution_id, job_name, _job_uid(job_obj))
            case WatchEventType.ADDED | WatchEventType.MODIFIED:
                await self._handle_job_update(execution_id, job_name, job_obj)

    async def _handle_job_deleted(
        self, execution_id: str, job_name: str, job_uid: str | None = None
    ) -> None:
        """Handle Job deletion event.

        Marks the execution as failed if it was still being tracked.
        Uses idempotent publishing to avoid overwriting completed status
        when cleanup deletes a successfully finished Job.

        A retry reuses the execution id, so ``start_container`` deletes the
        previous attempt's Job before recreating one under the same name
        (:meth:`_purge_stale_resources`). That fires a DELETED event naming
        a Job the new attempt has already replaced; the UID tells the two
        apart, so a superseded generation never fails the run that took
        over its name.
        """
        if job_uid and not await self._is_current_job_uid(execution_id, job_uid):
            logger.debug(
                "Ignoring DELETED event for %s — a newer Job generation holds the name",
                job_name,
            )
            return

        await self.publish_status_idempotent(
            execution_id=execution_id,
            status="failed",
            container_id=job_name,
            error_type="container_error",
            error_message="Job was deleted before completion",
        )

    async def _handle_job_update(
        self,
        execution_id: str,
        job_name: str,
        job_obj: Any,
    ) -> None:
        """Handle Job addition or modification event.

        Checks for terminal states and processes exit if found.
        """
        exit_code = self._get_job_terminal_state(job_obj)
        if exit_code is not None:
            await self._handle_job_exit(
                execution_id=execution_id,
                job_name=job_name,
                exit_code=exit_code,
            )

    async def _handle_job_exit(
        self,
        execution_id: str,
        job_name: str,
        exit_code: int,
    ) -> None:
        """Handle Job exit event with idempotency.

        Uses publish_status_idempotent to prevent duplicate processing.
        """
        # Cheap early-out before the pod-log round trip: an execution no
        # longer in the active set has already been processed (startup
        # replay of every terminal Job, duplicate watch events).
        # ``publish_status_idempotent`` is still the authoritative gate.
        if not await self._redis.sismember(self.active_executions_key, execution_id):
            logger.debug("Skipping already-processed execution %s", execution_id)
            return

        result, error_info, rate_limit_events = await self._get_pod_logs_result(job_name)
        status, final_error_info, retry_at = await self.resolve_exit_outcome(
            execution_id, exit_code, error_info, rate_limit_events
        )

        published = await self.publish_status_idempotent(
            execution_id=execution_id,
            status=status,
            container_id=job_name,
            exit_code=exit_code,
            error_type=final_error_info[0] if final_error_info else None,
            error_message=final_error_info[1] if final_error_info else None,
            result=result,
            retry_at=retry_at,
        )

        if published:
            await self._cleanup_job(job_name)

    # === Reconciliation ===

    async def reconcile(self) -> None:
        """Three-phase reconciliation scoped to this plugin's Jobs.

        1. ALL terminal Jobs → force-publish status (bypasses idempotency).
           After a restart, the execution may have been removed from Redis
           but the pub/sub message was lost. The idempotent path would skip
           these forever, so reconciliation bypasses it.
        2. Orphaned executions (in Redis but Job gone) → publish failed.
        3. Stale DB executions (queued/processing in DB but no running pod).
        """
        try:
            api = await self._get_api()
            label_selector = {LABEL_PLUGIN: self._plugin_name}

            active_executions = await self.get_active_executions()

            # Build mapping: execution_id -> Job
            job_map: dict[str, Any] = {}
            async for job_obj in Job.list(
                namespace=self._config.namespace,
                label_selector=label_selector,
                api=api,
            ):
                exec_id = (job_obj.labels or {}).get(LABEL_EXECUTION_ID)
                if exec_id:
                    job_map[exec_id] = job_obj

            # Phase 1: Process terminal Jobs whose execution is still tracked
            # as in-flight (in Redis, or queued/running in the DB) but whose
            # pub/sub completion message was lost. Jobs whose execution is
            # already resolved are skipped — re-publishing their status every
            # cycle is what exhausts the DB connection pool once finished
            # Jobs pile up (cleanup disabled in dev).
            terminal: list[tuple[str, Any, int]] = []
            for exec_id, job_obj in job_map.items():
                exit_code = self._get_job_terminal_state(job_obj)
                if exit_code is not None:
                    terminal.append((exec_id, job_obj, exit_code))

            unknown_terminal = {e for e, _job, _code in terminal if e not in active_executions}
            pending = await self.executions_pending_reconcile(unknown_terminal)

            for exec_id, job_obj, exit_code in terminal:
                if exec_id not in active_executions and exec_id not in pending:
                    continue
                status_label = "completed" if exit_code == 0 else "failed"
                logger.info(
                    "Reconciling %s Job %s for execution %s (plugin=%s)",
                    status_label,
                    job_obj.name,
                    exec_id,
                    self._plugin_name,
                )
                await self._reconcile_job_exit(
                    execution_id=exec_id,
                    job_name=job_obj.name,
                    exit_code=exit_code,
                )

            # Phase 2: Orphaned executions (in Redis but no Job)
            job_exec_ids = set(job_map.keys())
            orphaned = active_executions - job_exec_ids

            for exec_id in orphaned:
                logger.warning(
                    "Found orphaned execution %s (plugin=%s) — Job not found",
                    exec_id,
                    self._plugin_name,
                )
                await self.unregister_execution(exec_id)
                await self.publish_status(
                    execution_id=exec_id,
                    status="failed",
                    error_type="container_error",
                    error_message="Job not found — may have been removed unexpectedly",
                )

            # Phase 3: Stale DB executions whose Job is no longer running.
            # Scoping to this plugin's executions happens inside
            # ``fail_stale_db_executions`` via ``Execution.provider`` (#104).
            running_exec_ids = {
                exec_id
                for exec_id, job_obj in job_map.items()
                if self._get_job_terminal_state(job_obj) is None
            }
            await self.fail_stale_db_executions(running_exec_ids)

        except Exception as e:
            logger.error("Error during reconciliation (plugin=%s): %s", self._plugin_name, e)

    async def _reconcile_job_exit(
        self,
        execution_id: str,
        job_name: str,
        exit_code: int,
    ) -> None:
        """Handle Job exit during reconciliation.

        Bypasses idempotency (safe under reconcile lock). Always publishes
        status as a safety net for lost pub/sub messages.
        """
        result, error_info, rate_limit_events = await self._get_pod_logs_result(job_name)
        status, final_error_info, retry_at = await self.resolve_exit_outcome(
            execution_id, exit_code, error_info, rate_limit_events
        )

        await self.unregister_execution(execution_id)

        await self.publish_status(
            execution_id=execution_id,
            status=status,
            container_id=job_name,
            exit_code=exit_code,
            error_type=final_error_info[0] if final_error_info else None,
            error_message=final_error_info[1] if final_error_info else None,
            result=result,
            retry_at=retry_at,
        )

        await self._cleanup_job(job_name)

    # === Internal Helpers ===

    def _build_job_manifest(
        self,
        job_name: str,
        names: _ResourceNames,
        request: ContainerRequest,
        *,
        public_env: dict[str, str],
        proxy: ProxySpec,
    ) -> dict:
        """Build the sandboxed Kubernetes Job manifest.

        Every pod is two-container: the agent (no credentials, points
        ``HTTPS_PROXY`` at the in-pod loopback proxy) and the
        security-proxy sidecar (owns all credentials, enforces the host
        allowlist).
        """
        sanitized_labels = {
            _sanitize_label_value(k): _sanitize_label_value(v) for k, v in request.labels.items()
        }

        agent_secret = names.agent_secret if public_env else None
        agent_container = self._build_agent_container(request, agent_secret)
        self._apply_sandbox_to_agent(agent_container, list(request.secrets.keys()))

        # Native K8s sidecar — startupProbe gates the agent so it can't race ahead.
        pod_spec: dict[str, Any] = {
            "initContainers": [self._build_security_proxy_container(proxy, names)],
            "containers": [agent_container],
            "restartPolicy": "Never",
            "serviceAccountName": self._config.service_account,
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": self._config.run_as_user,
                "runAsGroup": self._config.run_as_group,
            },
            "volumes": [
                {
                    "name": "tmp",
                    "emptyDir": {"sizeLimit": request.tmp_size_limit or _FALLBACK_TMP_SIZE_LIMIT},
                },
                *_sandbox_volumes(names),
            ],
        }

        if self._config.image_pull_secrets:
            pod_spec["imagePullSecrets"] = [
                {"name": name} for name in self._config.image_pull_secrets
            ]

        if self._config.node_selector:
            pod_spec["nodeSelector"] = self._config.node_selector

        if self._config.tolerations:
            pod_spec["tolerations"] = self._config.tolerations

        # Build template metadata
        template_metadata: dict[str, Any] = {"labels": sanitized_labels}
        if self._config.annotations:
            template_metadata["annotations"] = self._config.annotations

        job_spec: dict[str, Any] = {
            "backoffLimit": 0,
            "activeDeadlineSeconds": request.timeout_seconds,
            "template": {
                "metadata": template_metadata,
                "spec": pod_spec,
            },
        }

        if self._config.cleanup_jobs:
            job_spec["ttlSecondsAfterFinished"] = _CLEANUP_TTL_SECONDS

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self._config.namespace,
                "labels": sanitized_labels,
            },
            "spec": job_spec,
        }

    def _build_agent_container(
        self,
        request: ContainerRequest,
        agent_secret_name: str | None,
    ) -> dict[str, Any]:
        """Build the worker container spec — env source decided by caller."""
        spec: dict[str, Any] = {
            "name": CONTAINER_NAME,
            "image": request.image,
            "imagePullPolicy": self._config.image_pull_policy,
            "resources": {
                "requests": {
                    "memory": self._config.memory_request,
                    "cpu": self._config.cpu_request,
                },
                "limits": {
                    "memory": self._config.memory_limit,
                    "cpu": self._config.cpu_limit,
                },
            },
            "securityContext": {
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
            },
            "volumeMounts": [
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
        if request.command:
            spec["args"] = request.command
        if agent_secret_name:
            spec["envFrom"] = [{"secretRef": {"name": agent_secret_name}}]
        return spec

    def _apply_sandbox_to_agent(
        self, agent_container: dict[str, Any], secret_keys: list[str]
    ) -> None:
        """In-place mutation: point the agent at the sidecar and trust the CA.

        The agent container deliberately receives no real credentials —
        outbound HTTPS is forced through the loopback proxy and the
        per-execution CA is wired into every common HTTP-client trust
        store the CLI uses.

        ``secret_keys`` lists the sidecar-only credentials. We seed each
        one on the agent with a placeholder string so tools that hard-
        require their auth env var at startup (Claude Code, gh, …) think
        they're configured. The proxy strips any ``Authorization`` /
        ``x-api-key`` headers the agent emits and injects the real values
        from the sidecar — the placeholder string never crosses the wire.
        """
        agent_container.setdefault("env", []).extend(
            [
                {"name": "HTTPS_PROXY", "value": SECURITY_PROXY_LOOPBACK},
                {"name": "HTTP_PROXY", "value": SECURITY_PROXY_LOOPBACK},
                {"name": "NO_PROXY", "value": "127.0.0.1,localhost"},
                {"name": "NODE_EXTRA_CA_CERTS", "value": SECURITY_PROXY_CA_PATH_AGENT},
                {"name": "SSL_CERT_FILE", "value": SECURITY_PROXY_CA_PATH_AGENT},
                {"name": "REQUESTS_CA_BUNDLE", "value": SECURITY_PROXY_CA_PATH_AGENT},
                {"name": "GIT_SSL_CAINFO", "value": SECURITY_PROXY_CA_PATH_AGENT},
                {"name": "CURL_CA_BUNDLE", "value": SECURITY_PROXY_CA_PATH_AGENT},
            ]
        )
        for key in secret_keys:
            agent_container["env"].append({"name": key, "value": SANDBOX_PLACEHOLDER})
        agent_container["volumeMounts"].append(
            {
                "name": "security-proxy-ca",
                "mountPath": SECURITY_PROXY_CA_PATH_AGENT,
                "subPath": "ca.crt",
                "readOnly": True,
            }
        )

    def _build_security_proxy_container(
        self,
        proxy: ProxySpec,
        names: _ResourceNames,
    ) -> dict[str, Any]:
        """Build the security-proxy sidecar — owner of all credentials."""
        return {
            "name": SECURITY_PROXY_CONTAINER_NAME,
            "image": proxy.image,
            "imagePullPolicy": self._config.image_pull_policy,
            "restartPolicy": "Always",
            "envFrom": [{"secretRef": {"name": names.proxy_secret}}],
            "resources": {
                "requests": {"memory": "64Mi", "cpu": "50m"},
                "limits": {"memory": "256Mi", "cpu": "500m"},
            },
            "securityContext": {
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            # Exec probe — tcpSocket goes to pod IP; mitmdump only binds 127.0.0.1.
            "startupProbe": {
                "exec": {
                    "command": [
                        "python3",
                        "-c",
                        "import socket,sys; s=socket.socket(); s.settimeout(1); "
                        "s.connect(('127.0.0.1', 8080)); s.close()",
                    ],
                },
                "periodSeconds": 1,
                "failureThreshold": 30,
            },
            "volumeMounts": [
                # ConfigMap mounted as a directory — its `config.json` key
                # becomes a file inside SECURITY_PROXY_CONFIG_DIR. We avoid
                # subPath here so it doesn't collide with the CA dir mount.
                {
                    "name": "security-proxy-config",
                    "mountPath": SECURITY_PROXY_CONFIG_DIR,
                    "readOnly": True,
                },
                {
                    "name": "security-proxy-ca-priv",
                    "mountPath": SECURITY_PROXY_CA_DIR_SIDECAR,
                    "readOnly": True,
                },
                {"name": "security-proxy-tmp", "mountPath": "/tmp"},
            ],
        }

    async def _get_pod_logs_result(
        self,
        job_name: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None, list[dict[str, Any]]]:
        """Get and parse pod logs for result/error markers."""
        try:
            api = await self._get_api()
            pods = [
                pod
                async for pod in Pod.list(
                    namespace=self._config.namespace,
                    label_selector=f"job-name={job_name}",
                    api=api,
                )
            ]

            if not pods:
                logger.warning("No pods found for Job %s", job_name)
                return None, None, []

            # Only the tail is needed — the CLI emits the [JEANCLODE:RESULT] /
            # [JEANCLODE:ERROR] markers right before exiting. Slurping a full
            # 30-minute agent log (tens of MB) into memory, especially for
            # many Jobs at once during reconcile, is the memory cost we avoid.
            log_lines: list[str] = []
            async for line in pods[0].logs(container=CONTAINER_NAME, tail_lines=_LOG_TAIL_LINES):
                log_lines.append(line)
            log_text = "\n".join(log_lines)
            return parse_structured_logs(log_text)

        except Exception as e:
            logger.error("Failed to parse pod logs for Job %s: %s", job_name, e)
            return None, None, []

    async def _cleanup_job(self, job_name: str) -> None:
        """Remove a Job after processing."""
        if not self._config.cleanup_jobs:
            logger.debug("Skipping cleanup for Job %s (cleanup disabled)", job_name)
            return

        try:
            api = await self._get_api()
            job = await Job.get(job_name, namespace=self._config.namespace, api=api)
            await job.delete(propagation_policy="Background")
            logger.debug("Cleaned up Job %s", job_name)
        except Exception as e:
            logger.warning("Failed to cleanup Job %s: %s", job_name, e)
