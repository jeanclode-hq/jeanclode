"""Shared webhook → execution dispatch logic for GitHub and GitLab.

Both providers' PR/MR webhook handlers normalize their event vocabulary
to ``opened`` / ``synchronize`` / ``labeled`` and call into this module
to decide whether to fire a REVIEW or SUMMARY workflow — always via the
``jeanclode:<workflow>`` label, never PR creation or a commit on its own.
Issue webhook handlers do the same for the ISSUE_RESOLVE workflow via
:func:`maybe_dispatch_issue_workflow`, gated by the ``jeanclode:resolve``
label.

Why this lives here and not in the per-provider plugin: the dispatch
event published downstream is provider-scoped
(``jeanclode.events.<provider>.manual_dispatch``), but the *decision* is
provider-agnostic — keeping it in one place avoids two copies of the
trigger matrix.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.execution import (
    db_create_execution,
    db_has_active_execution,
    db_update_execution_status,
)
from api.database.organization import db_get_org_by_id
from api.database.pull_request import db_has_active_pr_execution
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.issues import Issue
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.models.settings import RepoSettings
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.plugins.github.dispatch import LABEL_RESOLVE, evaluate_trigger
from api.sse.publishers import publish_pull_request_event

logger = logging.getLogger(__name__)


# Workflows that webhook events can auto-trigger on PRs/MRs. FIX is
# intentionally absent — fixes are issue-driven and dispatched via the
# Sentry plugin path.
_PR_AUTO_WORKFLOWS = (ExecutionWorkflow.REVIEW, ExecutionWorkflow.SUMMARY)


def repo_is_enabled(repo: Repository | None) -> bool:
    """Whether the repo the event landed on has triggers turned on.

    A disabled repo is inert as an *entry point*: no webhook starts a run on
    it. It stays a valid group member, so a run started from an enabled repo
    still clones it (see ``db_get_related_repos``).
    """
    if repo is None:
        return False
    return RepoSettings.model_validate(repo.settings or {}).enabled


async def maybe_dispatch_pr_workflows(
    *,
    pr_id: UUID,
    org_id: UUID,
    provider: str,
    action: str,
    label_added: str | None = None,
) -> None:
    """Evaluate REVIEW + SUMMARY triggers and dispatch any that match.

    Nothing fires on a repo the tenant disabled. Idempotent: skips
    workflows that already have a queued/running execution on this PR.
    Safe to call from any webhook event — the evaluator returns False for
    anything but its own ``jeanclode:<workflow>`` label being added.

    Takes ids rather than a session: the executions are reserved in one
    short unit of work, and the stream publishes that follow happen with
    the pooled connection already back.
    """

    def _reserve(db: Session) -> tuple[str, list[tuple[str, str]]] | None:
        """Create an execution per matching workflow, still to be published."""
        pr = db.get(PullRequest, pr_id)
        if pr is None:
            return None

        if not repo_is_enabled(pr.repository):
            logger.debug("Skipping auto PR workflows for PR %s — repository is disabled", pr_id)
            return None

        org = db_get_org_by_id(db, org_id)
        if not org:
            return None

        workspace_id = str(org.workspace_id) if org.workspace_id else ""
        reserved: list[tuple[str, str]] = []
        for workflow in _PR_AUTO_WORKFLOWS:
            if not evaluate_trigger(workflow, action=action, label_added=label_added):
                continue

            if db_has_active_pr_execution(db, pr.id, workflow=workflow.value):
                logger.debug(
                    "Skipping auto %s for PR %s — active execution already exists",
                    workflow.value,
                    pr_id,
                )
                continue

            execution = db_create_execution(
                db,
                provider=provider,
                pull_requests=[pr],
                workflow=workflow.value,
                trigger=ExecutionTrigger.AUTO.value,
                status=ExecutionStatus.QUEUED.value,
            )
            reserved.append((workflow.value, str(execution.id)))
        return workspace_id, reserved

    outcome = await run_in_session(_reserve)
    if outcome is None:
        return
    workspace_id, reserved = outcome

    broker = get_faststream_broker()
    stream = f"jeanclode.events.{provider}.manual_dispatch"

    for workflow_value, execution_id in reserved:
        try:
            await broker.publish(
                {
                    "pull_request_id": str(pr_id),
                    "execution_id": execution_id,
                    "organization_id": str(org_id),
                    "workflow": workflow_value,
                },
                stream=stream,
                maxlen=STREAM_MAXLEN,
            )
            await publish_pull_request_event(
                workspace_id=workspace_id,
                action="execution_created",
                payload={
                    "pull_request_id": str(pr_id),
                    "execution_id": execution_id,
                    "status": ExecutionStatus.QUEUED.value,
                    "workflow": workflow_value,
                    "trigger": ExecutionTrigger.AUTO.value,
                },
            )
        except Exception:
            logger.exception(
                "Auto-dispatch publish failed for execution %s — marking FAILED",
                execution_id,
            )

            def _mark_failed(db: Session, eid: str = execution_id) -> None:
                db_update_execution_status(
                    db,
                    UUID(eid),
                    ExecutionStatus.FAILED.value,
                    error_type="broker_publish_failed",
                    error_detail="Failed to enqueue dispatch event",
                )

            await run_in_session(_mark_failed)
            continue

        logger.info(
            "Auto %s queued for PR %s (provider=%s, action=%s, label_added=%s)",
            workflow_value,
            pr_id,
            provider,
            action,
            label_added,
        )


async def maybe_dispatch_issue_workflow(
    *,
    issue_id: UUID,
    org_id: UUID,
    provider: str,
    action: str,
    label_added: str | None = None,
) -> None:
    """Dispatch issue-resolve when the ``jeanclode:resolve`` label is added.

    The label is an explicit ask — added by a user, or by the respond
    workflow on a resolve-intent mention. Nothing else triggers this
    workflow from a webhook (an issue opening on its own never does).
    Doesn't fire on a repo the tenant disabled.

    Idempotent: skips when the issue already has an active execution.
    """

    def _reserve(db: Session) -> str | None:
        """Create the execution to publish, or ``None`` when nothing should fire."""
        issue = db.get(Issue, issue_id)
        if issue is None:
            return None

        if not repo_is_enabled(issue.repository):
            logger.debug("Skipping issue_resolve for issue %s — repository is disabled", issue_id)
            return None

        org = db_get_org_by_id(db, org_id)
        if not org:
            return None

        if action != "labeled" or label_added != LABEL_RESOLVE:
            return None

        if db_has_active_execution(db, issue.id, workflow=ExecutionWorkflow.ISSUE_RESOLVE.value):
            logger.debug(
                "Skipping issue_resolve for issue %s — active execution already exists",
                issue_id,
            )
            return None

        execution = db_create_execution(
            db,
            provider=provider,
            issues=[issue],
            workflow=ExecutionWorkflow.ISSUE_RESOLVE.value,
            trigger=ExecutionTrigger.AUTO.value,
            status=ExecutionStatus.QUEUED.value,
        )
        return str(execution.id)

    execution_id = await run_in_session(_reserve)
    if execution_id is None:
        return

    try:
        await get_faststream_broker().publish(
            {
                "issue_id": str(issue_id),
                "execution_id": execution_id,
                "organization_id": str(org_id),
                "workflow": ExecutionWorkflow.ISSUE_RESOLVE.value,
            },
            stream=f"jeanclode.events.{provider}.issue_resolve",
            maxlen=STREAM_MAXLEN,
        )
    except Exception:
        logger.exception(
            "Issue-resolve dispatch publish failed for execution %s — marking FAILED",
            execution_id,
        )
        await run_in_session(
            lambda db: db_update_execution_status(
                db,
                UUID(execution_id),
                ExecutionStatus.FAILED.value,
                error_type="broker_publish_failed",
                error_detail="Failed to enqueue dispatch event",
            )
        )
        return

    logger.info(
        "Auto issue_resolve queued for issue %s (provider=%s, action=%s, label_added=%s)",
        issue_id,
        provider,
        action,
        label_added,
    )
