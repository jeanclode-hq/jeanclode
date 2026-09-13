"""Sentry issue dispatch — polls pending issues and dispatches CLI containers.

Dispatch partitions by the **git org that owns the mapped target repo**: one
Sentry org's projects can map into several git orgs, a container gets exactly
one git-platform token, so each ``(sentry_org, git_org)`` pair is an
independent batch — its own window, its own merge gate, dispatched
concurrently with the others.

The **git org is the concurrency perimeter**: only one fix batch runs per git
org at a time. While a fixer container for a git org is still working, the
dispatcher starts nothing new for that git org — a second concurrent fixer
there would double that org's container and LLM load and race the first on
the same repos. Different git orgs are unaffected.
"""

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database.execution import (
    db_create_execution,
    db_get_dispatchable_issues,
    db_get_eligible_dispatch_targets,
    db_git_org_has_running_fix_execution,
)
from api.database.llm_credentials import LLMCredentialAvailability
from api.database.organization import db_claim_dispatch_window, db_get_org_by_id
from api.database.pull_request import db_git_org_has_open_fix_pr
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.issues import Issue
from api.models.organizations import Organization, Provider
from api.models.settings import batch_size_value, batch_window_minutes
from api.plugins.container.dispatch_inputs import (
    check_llm_availability,
    resolve_memory_workspace_id,
)
from api.plugins.sentry.config import SentryDispatchConfig
from api.plugins.sentry.fix_pr_reconcile import reconcile_open_fix_prs
from api.plugins.sentry.launch import launch_container

logger = logging.getLogger(__name__)


class SentryDispatcher:
    """Background loop that polls for pending Sentry issues and dispatches containers."""

    def __init__(self, config: SentryDispatchConfig) -> None:
        self._config = config
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Start the dispatch background loop."""
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Sentry dispatcher started (interval=%ds, batch=%d)",
            self._config.interval_seconds,
            self._config.batch_size,
        )

    async def stop(self) -> None:
        """Stop the dispatch background loop."""
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Sentry dispatcher stopped")

    def health_check(self) -> dict[str, Any]:
        """Check dispatcher health."""
        task_alive = self._task is not None and not self._task.done()
        return {"healthy": task_alive, "stopping": self._stopping.is_set()}

    # ── Internal loop ─────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop — tick every interval_seconds, exit on stop signal."""
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Sentry dispatch tick failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._config.interval_seconds,
                )
                break
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        """One dispatch cycle: find eligible (sentry_org, git_org) pairs, fan out."""
        app = get_current_app()
        db_plugin = app.database
        if not db_plugin:
            return

        with db_plugin.session() as db:
            targets = db_get_eligible_dispatch_targets(
                db,
                provider=Provider.SENTRY.value,
                max_retries=self._config.max_retries,
            )

        if not targets:
            return

        logger.info("Dispatching for %d eligible target(s)", len(targets))

        semaphore = asyncio.Semaphore(self._config.max_concurrent_dispatches)

        async def _bounded_dispatch(pair: tuple[UUID, UUID]) -> None:
            async with semaphore:
                await self._dispatch_target(*pair)

        results = await asyncio.gather(
            *[_bounded_dispatch(pair) for pair in targets],
            return_exceptions=True,
        )
        for pair, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                logger.exception("Dispatch failed for target %s", pair, exc_info=result)

    async def _dispatch_target(self, sentry_org_id: UUID, git_org_id: UUID) -> None:
        """Reconcile, claim the git-org window, grab a pair-scoped batch, dispatch."""
        app = get_current_app()
        db_plugin = app.database
        if not db_plugin:
            return

        settings = await db_plugin.run_in_session(
            lambda db: _load_sentry_settings(db, sentry_org_id)
        )
        if settings is None:
            return

        # The git org is the perimeter: don't start a batch for a git org that
        # already has a fixer running. Checked before the window is claimed so
        # a long fixer doesn't burn this org's window while it runs. The
        # eligibility query filters on this too — this re-check closes the gap
        # between that query and now.
        has_running = await db_plugin.run_in_session(
            lambda db: db_git_org_has_running_fix_execution(db, git_org_id)
        )
        if has_running:
            logger.info(
                "Sentry dispatch for git org %s held — a fix batch is still running", git_org_id
            )
            return

        gate_enabled = bool(settings.get("gate_on_open_fix_prs", False))
        if gate_enabled:
            # Repair any dashboard/gate staleness from missed PR webhooks
            # before deciding whether this git org is clear to dispatch.
            await reconcile_open_fix_prs(db_plugin, git_org_id)

        window_minutes = batch_window_minutes(settings.get("batch_window"))
        claimed = await db_plugin.run_in_session(
            lambda db: db_claim_dispatch_window(db, git_org_id, window_minutes)
        )
        if not claimed:
            return

        batch_size = batch_size_value(settings.get("batch_size"), self._config.batch_size)

        def _claim_batch(db: Session) -> tuple[list[Issue], Organization, Any] | None:
            """Reserve a batch and its execution, detached for the launcher."""
            # The window claim above is the per-git-org mutex across pods: only
            # the pod that won the window reaches here, so the running-fix and
            # merge-gate re-checks below need no extra lock — they only guard
            # against a batch from an earlier window still being in flight.
            if db_git_org_has_running_fix_execution(db, git_org_id):
                logger.info(
                    "Sentry dispatch for git org %s held — a fix batch is still running",
                    git_org_id,
                )
                return None

            org = db_get_org_by_id(db, sentry_org_id)
            if not org:
                return None

            issues = db_get_dispatchable_issues(
                db,
                sentry_org_id,
                batch_size,
                self._config.max_retries,
                git_org_id=git_org_id,
            )
            if not issues:
                return None

            # Merge gate (Part E2), re-checked here in case a PR opened
            # between the eligibility query and now. A forgotten open bot PR
            # halts this git org's Sentry fixes until someone deals with it.
            if gate_enabled and db_git_org_has_open_fix_pr(db, git_org_id):
                logger.info(
                    "Sentry dispatch for git org %s held — an open fix PR remains",
                    git_org_id,
                )
                return None

            # Real, temporary exhaustion (every credential currently stale)
            # never enters the execution/retry-count machinery at all for
            # Sentry — leave these issues untouched (no execution created)
            # so the next eligible tick naturally retries. A misconfigured
            # (empty) pool is different: still create the execution below so
            # launch_container's normal fail-fast surfaces it to the admin.
            availability = check_llm_availability()
            if availability.availability == LLMCredentialAvailability.ALL_STALE:
                logger.info(
                    "Skipping sentry dispatch for org %s — every LLM credential is stale (retry_at=%s)",
                    sentry_org_id,
                    availability.retry_at,
                )
                return None

            # db_create_execution commits, and an expiring commit would leave
            # the issues and org needing a reload the moment the launcher
            # reads them — by which point the session is gone. Keep them
            # populated so they survive being detached below.
            db.expire_on_commit = False
            execution = db_create_execution(
                db,
                provider="sentry",
                issues=issues,
                workflow=ExecutionWorkflow.FIX.value,
                status=ExecutionStatus.QUEUED.value,
            )

            # The FOR UPDATE lock these issues were read under ends with this
            # session, but the committed FIX execution is what actually makes
            # them undispatchable, so a concurrent tick still skips them.
            db.expunge_all()
            return issues, org, execution.id

        batch = await db_plugin.run_in_session(_claim_batch)
        if batch is None:
            return
        issues, org, execution_id = batch

        workspace_id = await resolve_memory_workspace_id(sentry_org_id)
        await launch_container(issues, org, execution_id, workspace_id=workspace_id)


def _load_sentry_settings(db: Session, sentry_org_id: UUID) -> dict[str, Any] | None:
    """The Sentry org's raw settings dict, or ``None`` if the org is gone."""
    org = db_get_org_by_id(db, sentry_org_id)
    if not org:
        return None
    return dict(org.settings or {})
