"""Container backend abstraction with built-in watching infrastructure.

Each backend instance is scoped to a plugin (e.g., "sentry") via plugin_name.
This scopes Redis keys, stream names, and container label filters so that
multiple plugins can run independent watchers without interference.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database.execution import db_get_execution_by_id, db_get_running_executions
from api.database.llm_credentials import (
    LLMCredentialAvailability,
    db_mark_llm_credential_stale,
    db_select_llm_credential,
)
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.plugins.container.rate_limit_cleanup import cleanup_sentry_fix_pr
from api.plugins.container.security_proxy import OAuthUpstream, UpstreamCredential
from api.plugins.container.utils import determine_execution_status
from api.services.llm_credentials import stale_until_from_retry_after

if TYPE_CHECKING:
    from faststream.redis import RedisBroker
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

STREAM_MAXLEN = 10000
RECONCILE_LOCK_TTL = 60  # seconds


class ContainerStatus(StrEnum):
    """Status of a container."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ContainerRequest(BaseModel):
    """Generic request to run a container.

    The K8s backend always sandboxes (sidecar + agent). Callers describe
    intent explicitly:

    * ``env``: non-secret configuration the agent itself needs (URLs the
      agent will format, log levels, etc.).
    * ``secrets``: credential env vars that go on the **sidecar**,
      keyed by the env var name the proxy config will reference. They
      never appear on the agent container.
    * ``upstreams``: per-credential allowlist + injection metadata. The
      dispatching plugin resolves the upstream host from whichever
      authoritative source it has (admin LLM config, org row, env, …)
      and emits one ``UpstreamCredential`` per allowed call.

    Anything outside ``upstreams`` is denied at L7 by the sidecar.
    """

    image: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    upstreams: list[UpstreamCredential] = Field(default_factory=list)
    # oauth2 client_credentials rules the sidecar mints and refreshes on
    # its own — see OAuthUpstream. Distinct from ``upstreams`` because the
    # proxy has to actively call out to a token endpoint, not just
    # template a static secret into a header.
    oauth_upstreams: list[OAuthUpstream] = Field(default_factory=list)
    # Hosts the agent may reach without any credential injection
    # (public CDNs like raw.githubusercontent.com that the Claude Code
    # subprocess fetches from). Each entry just contributes to the proxy
    # allowlist; nothing is added to the request headers.
    extra_hosts: list[str] = Field(default_factory=list)
    timeout_seconds: int = 1800
    labels: dict[str, str] = Field(default_factory=dict)
    # Kubernetes emptyDir sizeLimit for /tmp — computed per-dispatch from
    # live repo-size lookups (see api.plugins.container.sizing). Ignored by
    # the Docker backend, which has no disk-size concept for containers.
    tmp_size_limit: str | None = None


class ContainerResult(BaseModel):
    """Result of a completed container run."""

    container_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    status: ContainerStatus


class ContainerBackend(ABC):
    """Abstract base for container backends (Docker, K8s).

    Each backend handles BOTH execution and watching for its platform.

    Features:
    - Plugin-scoped Redis keys and streams
    - Redis-based tracking of active executions (no DB polling)
    - Idempotent status updates (atomic SREM gate)
    - Distributed locking for reconciliation
    """

    def __init__(self, plugin_name: str, broker: RedisBroker, redis: Redis) -> None:
        """Initialize the backend.

        Args:
            plugin_name: Plugin that owns this backend (e.g., "sentry").
            broker: FastStream Redis broker for publishing status updates.
            redis: Redis client for execution tracking and locking.
        """
        self._plugin_name = plugin_name
        self._broker = broker
        self._redis = redis

    # === Plugin-scoped Redis keys ===

    @property
    def active_executions_key(self) -> str:
        """Redis set key for tracking active executions."""
        return f"jeanclode:{self._plugin_name}:executions:active"

    @property
    def reconcile_lock_key(self) -> str:
        """Redis key for distributed reconciliation lock."""
        return f"jeanclode:{self._plugin_name}:reconcile:lock"

    @property
    def status_stream(self) -> str:
        """Redis stream name for execution status updates."""
        return f"jeanclode.{self._plugin_name}.execution.status"

    # === Execution Tracking (Redis-based) ===

    async def register_execution(self, execution_id: str, container_id: str | None = None) -> bool:
        """Register an execution as active.

        Uses SADD as an atomic gate — returns True only for the first caller.

        Args:
            execution_id: Execution ID to track.
            container_id: Optional container/job ID.

        Returns:
            True if this was the first registration.
        """
        added = await self._redis.sadd(self.active_executions_key, execution_id)
        if added:
            await self.publish_status(
                execution_id=execution_id,
                status="running",
                container_id=container_id,
            )
        logger.debug(f"Registered execution {execution_id} as active (new={bool(added)})")
        return bool(added)

    async def unregister_execution(self, execution_id: str) -> None:
        """Remove an execution from active set in Redis.

        Args:
            execution_id: Execution ID to remove.
        """
        await self._redis.srem(self.active_executions_key, execution_id)
        logger.debug(f"Unregistered execution {execution_id}")

    async def get_active_executions(self) -> set[str]:
        """Get all active execution IDs from Redis.

        Returns:
            Set of execution IDs.
        """
        members = await self._redis.smembers(self.active_executions_key)
        return {m.decode() if isinstance(m, bytes) else m for m in members}

    # === Distributed Locking ===

    async def acquire_reconcile_lock(self) -> bool:
        """Try to acquire the reconcile lock.

        Only one instance should reconcile at a time.

        Returns:
            True if lock acquired, False if already held by another instance.
        """
        acquired = await self._redis.set(
            self.reconcile_lock_key,
            "1",
            nx=True,
            ex=RECONCILE_LOCK_TTL,
        )
        return bool(acquired)

    async def release_reconcile_lock(self) -> None:
        """Release the reconcile lock."""
        await self._redis.delete(self.reconcile_lock_key)

    # === Status Publishing ===

    async def publish_status(
        self,
        execution_id: str,
        status: str,
        container_id: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        result: dict[str, Any] | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        """Publish a status update to the plugin-scoped Redis stream.

        Args:
            execution_id: ID of the execution.
            status: New status (processing, completed, failed, scheduled).
            container_id: Optional container ID.
            exit_code: Optional exit code.
            error_message: Optional error message.
            error_type: Optional standardized error type.
            result: Optional parsed result from container logs.
            retry_at: Set only when ``status == "scheduled"`` (ADR-010) —
                when every LLM credential was found stale mid-run.
        """
        message = {
            "execution_id": execution_id,
            "status": status,
            "container_id": container_id,
            "exit_code": exit_code,
            "error_message": error_message,
            "error_type": error_type,
            "result": result,
            "retry_at": retry_at.isoformat() if retry_at else None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self._broker.publish(message, stream=self.status_stream, maxlen=STREAM_MAXLEN)

    async def publish_status_idempotent(
        self,
        execution_id: str,
        status: str,
        container_id: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        result: dict[str, Any] | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        """Publish status update only if execution is still active.

        Uses atomic SREM as an idempotency gate — prevents duplicate
        processing when both event stream and reconciliation try to
        process the same container exit.

        Returns:
            True if status was published, False if skipped (already processed).
        """
        if status in ("completed", "failed", "scheduled"):
            removed = await self._redis.srem(self.active_executions_key, execution_id)
            if not removed:
                logger.debug(f"Skipping status update for {execution_id} - already processed")
                return False

        await self.publish_status(
            execution_id=execution_id,
            status=status,
            container_id=container_id,
            exit_code=exit_code,
            error_message=error_message,
            error_type=error_type,
            result=result,
            retry_at=retry_at,
        )
        return True

    # === Rate-limit handling (ADR-010) ===

    async def resolve_exit_outcome(
        self,
        execution_id: str,
        exit_code: int,
        error_info: tuple[str, str] | None,
        rate_limit_events: list[dict[str, Any]],
    ) -> tuple[str, tuple[str, str] | None, datetime | None]:
        """Determine the effective (status, error_info, retry_at) for an exit.

        When the CLI flagged a real rate-limit event, this takes over from
        the ordinary exit-code-based determination: the used credential is
        marked stale, and github/gitlab executions are redirected to
        ``scheduled`` (retried by the poller) instead of ``failed``. Sentry
        executions stay on the ordinary ``failed`` path — its own 30-second
        dispatch tick already retries a failed execution up to the retry
        cap, so nothing new is needed there (ADR-010).
        """
        if rate_limit_events:
            retry_at = await self._handle_rate_limit(execution_id, rate_limit_events)
            if retry_at is not None:
                return "scheduled", None, retry_at
        status, final_error_info = determine_execution_status(exit_code, error_info)
        return status, final_error_info, None

    async def _handle_rate_limit(
        self, execution_id: str, rate_limit_events: list[dict[str, Any]]
    ) -> datetime | None:
        """Mark the credential stale; return the retry_at for github/gitlab.

        A single container can run multiple groups concurrently (e.g.
        ``sentry_fix``'s parallel synthesis groups), each independently
        hitting the shared credential and emitting its own rate-limit event
        — so this processes every event, not just one. All events share the
        same credential (one container, one credential), so staleness is
        derived from the first; for ``sentry_fix`` every event's draft PR
        still needs its own cleanup.

        ``retry_at`` is derived from the pool's state *after* marking the
        failing credential stale, not from that credential's own
        ``stale_until`` — a lower-priority credential can already be usable
        (e.g. an API key sitting behind an exhausted OAuth subscription),
        in which case the retry shouldn't wait for this credential's window
        at all, only for the poller's next tick.

        Returns ``None`` for Sentry executions (no redirect needed — see
        :meth:`resolve_exit_outcome`) and when the execution can't be
        resolved at all.
        """
        app = get_current_app()
        db_plugin = app.database
        if not db_plugin:
            return None

        primary = rate_limit_events[0]
        credential_id = primary.get("credential_id")
        stale_until = stale_until_from_retry_after(primary.get("retry_after"))

        try:
            exec_uuid = UUID(execution_id)
        except ValueError:
            logger.warning("Rate-limit handling: invalid execution id %s", execution_id)
            return None

        with db_plugin.session() as db:
            if credential_id:
                try:
                    marked = db_mark_llm_credential_stale(
                        db, UUID(credential_id), stale_until=stale_until
                    )
                    if not marked:
                        logger.warning(
                            "Rate-limit handling: credential %s not found", credential_id
                        )
                except ValueError:
                    logger.warning("Rate-limit handling: invalid credential id %s", credential_id)
            else:
                logger.info(
                    "Rate-limit event for execution %s carried no credential_id "
                    "(likely an env-var-configured credential, outside the pool)",
                    execution_id,
                )

            execution = db_get_execution_by_id(db, exec_uuid)
            if not execution:
                return None
            provider = execution.provider
            workflow = execution.workflow

            # Re-walk the pool now that the failing credential is stale:
            # a different, lower-priority credential may already be usable.
            availability, _, pool_retry_at = db_select_llm_credential(db)

        if provider == "sentry":
            if workflow == ExecutionWorkflow.FIX.value:
                seen_pr_urls: set[str] = set()
                for rate_limit_info in rate_limit_events:
                    pr_url = rate_limit_info.get("pr_url")
                    if pr_url and pr_url in seen_pr_urls:
                        continue
                    if pr_url:
                        seen_pr_urls.add(pr_url)
                    await cleanup_sentry_fix_pr(exec_uuid, rate_limit_info)
            return None

        if availability == LLMCredentialAvailability.AVAILABLE:
            # Another credential is usable right now — don't wait out the
            # exhausted one's window, just let the next poller tick
            # redispatch immediately.
            return datetime.now(UTC)
        if availability == LLMCredentialAvailability.ALL_STALE:
            # pool_retry_at is the earliest stale_until across every
            # credential, which may differ from this one's own.
            return pool_retry_at
        # NONE_CONFIGURED: the failing credential lives outside the pool
        # (env-var configured) and there's nothing to re-walk — fall back
        # to the retry-after/default-window guess computed above.
        return stale_until

    # === Reconciliation helpers ===

    async def executions_pending_reconcile(self, candidate_ids: set[str]) -> set[str]:
        """Of ``candidate_ids``, which are still queued/running in the DB.

        Reconcile's "force-publish every terminal Job" phase only needs to
        act on executions still tracked as in-flight whose Job actually
        finished and whose pub/sub completion message was lost. Everything
        already resolved (terminal, scheduled for redispatch, or gone) is
        skipped — re-publishing its status every cycle spawns a
        status-consumer coroutine that opens a DB session, and a backlog of
        undeleted Jobs turns that into pool exhaustion.

        One batched query, run off the event loop: a synchronous per-job
        lookup here would block the loop and, under pool pressure, freeze it
        for ``pool_timeout`` seconds — long enough to trip the liveness
        probe. On any error, returns ``candidate_ids`` unchanged so the
        caller still processes every Job (pre-guard behaviour).
        """
        if not candidate_ids:
            return set()
        try:
            return await asyncio.to_thread(self._query_pending_executions, candidate_ids)
        except Exception:
            logger.exception("reconcile: could not read execution statuses")
            return set(candidate_ids)

    @staticmethod
    def _query_pending_executions(candidate_ids: set[str]) -> set[str]:
        app = get_current_app()
        if not app.database:
            return set(candidate_ids)
        uuids: list[UUID] = []
        for cid in candidate_ids:
            try:
                uuids.append(UUID(cid))
            except ValueError:
                continue
        if not uuids:
            return set()
        pending_statuses = (ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value)
        with app.database.session() as db:
            rows = db.execute(
                select(Execution.id, Execution.status).where(Execution.id.in_(uuids))
            ).all()
        return {str(row.id) for row in rows if row.status in pending_statuses}

    # === Stale Execution Cleanup ===

    async def fail_stale_db_executions(self, running_exec_ids: set[str]) -> None:
        """Fail this plugin's queued/running DB executions whose container is gone.

        Scoped to ``Execution.provider == self._plugin_name`` so a github
        reconciler tick cannot mark sentry-owned executions stale and vice
        versa (#104).

        Only considers executions older than 60s to give freshly queued jobs
        time to start their container.

        Args:
            running_exec_ids: Set of execution IDs that have a running container.
        """
        try:
            app = get_current_app()
            if not app.database:
                return
            grace_cutoff = datetime.now(UTC) - timedelta(seconds=60)

            def _collect_orphaned(db: Session) -> list[tuple[str, str]]:
                orphaned: list[tuple[str, str]] = []
                for execution in db_get_running_executions(
                    db,
                    provider=self._plugin_name,
                    exclude_ids=running_exec_ids,
                ):
                    # Skip recently created executions — container may still be starting
                    created_at = (
                        execution.created_at.replace(tzinfo=UTC)
                        if execution.created_at.tzinfo is None
                        else execution.created_at
                    )
                    if created_at > grace_cutoff:
                        continue
                    orphaned.append((str(execution.id), execution.status))
                return orphaned

            orphaned = await app.database.run_in_session(_collect_orphaned)

            for exec_id, status in orphaned:
                logger.warning(
                    f"Failing orphaned DB execution {exec_id} "
                    f"(status={status}, no running container)"
                )

                # Publish to stream — consumer handles DB update + SSE
                await self.publish_status(
                    execution_id=exec_id,
                    status="failed",
                    error_type="container_error",
                    error_message="Container no longer running — cleaned up or crashed",
                )

            if orphaned:
                logger.info(f"Failed {len(orphaned)} orphaned DB execution(s)")

        except Exception as e:
            logger.error(f"Error failing stale DB executions: {e}")

    # === Abstract methods ===

    @abstractmethod
    async def start_container(self, execution_id: str, request: ContainerRequest) -> str:
        """Start a container for the given execution.

        Implementations should call register_execution() after starting.

        Args:
            execution_id: Unique ID for this execution.
            request: Container configuration.

        Returns:
            Container ID.
        """

    @abstractmethod
    async def stop_container(self, container_id: str) -> None:
        """Stop a running container.

        Args:
            container_id: ID of the container to stop.
        """

    @abstractmethod
    async def start_watching(self) -> None:
        """Start watching for container events.

        This should start a background loop that monitors all containers
        with the plugin's labels and publishes status updates.
        """

    @abstractmethod
    async def stop_watching(self) -> None:
        """Stop watching for container events."""

    @abstractmethod
    async def reconcile(self) -> None:
        """Reconcile container state with Redis tracking and database.

        Handles:
        1. Containers that exited but weren't processed (event missed)
        2. Executions tracked in Redis but container is gone (orphaned)
        3. Stale DB executions (queued/processing in DB but no container)

        Called periodically by the plugin's reconcile loop.
        Must acquire reconcile lock before running.
        """

    # === Optional overridable methods ===

    async def run(self, request: ContainerRequest) -> ContainerResult:
        """Run a container to completion and return the result.

        Default raises NotImplementedError — override in backends that support it.
        """
        raise NotImplementedError("This backend does not support synchronous run()")

    async def get_status(self, container_id: str) -> ContainerStatus:
        """Get the current status of a container."""
        raise NotImplementedError

    async def get_logs(self, container_id: str) -> str:
        """Get logs from a running or stopped container."""
        raise NotImplementedError

    async def health_check(self) -> bool:
        """Check if the backend connection is healthy."""
        raise NotImplementedError
