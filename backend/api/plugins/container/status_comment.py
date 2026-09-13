"""Sticky status comment — one bot comment per PR/issue tracking every workflow.

Ported from the predecessor project's status-comment mechanism and
extended to cover Issues (not just PRs/MRs) and the ``respond`` /
``issue_resolve`` workflows (the old repo only had review/summary).

Each sync call is guarded by a per-target Redis lock so concurrent
execution-status updates for the same PR/issue can't race into duplicate
comments — combined with the marker-based find-or-update in
``platform_comments.py``, there is never more than one status comment per
target, and no comment ID needs to be persisted anywhere.
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from api.context import get_current_app
from api.database.execution import (
    db_get_latest_executions_for_issue_targets,
    db_get_latest_executions_for_pr,
)
from api.database.issue import db_get_issue_by_id
from api.database.organization import db_get_org_by_id
from api.database.repository import db_get_repository_by_id
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.pull_requests import PullRequest
from api.plugins.container.platform_comments import upsert_github_comment, upsert_gitlab_note

logger = logging.getLogger(__name__)

MARKER = "<!-- jeanclode-status -->"

_WORKFLOW_DISPLAY: dict[str, str] = {
    ExecutionWorkflow.FIX.value: "Fix",
    ExecutionWorkflow.REVIEW.value: "Review",
    ExecutionWorkflow.SUMMARY.value: "Summary",
    ExecutionWorkflow.RESPOND.value: "Respond",
    ExecutionWorkflow.ISSUE_RESOLVE.value: "Resolve",
}

# FIX links an Issue at creation and gains a PR once it opens one, so it's
# relevant to both the issue-side and PR-side comment.
_PR_WORKFLOWS = (
    ExecutionWorkflow.FIX.value,
    ExecutionWorkflow.REVIEW.value,
    ExecutionWorkflow.SUMMARY.value,
    ExecutionWorkflow.RESPOND.value,
)
_ISSUE_WORKFLOWS = (
    ExecutionWorkflow.FIX.value,
    ExecutionWorkflow.ISSUE_RESOLVE.value,
    ExecutionWorkflow.RESPOND.value,
)

_ACTIVE_STATUSES = frozenset({ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value})

_LOCK_TIMEOUT = 30
_LOCK_BLOCKING_TIMEOUT = 10


def build_status_body(latest: Mapping[str, Execution]) -> str | None:
    """Build the markdown body for the sticky status comment.

    Buckets the latest execution per workflow into completed / running /
    errored. Returns ``None`` when there's nothing to report yet.
    """
    if not latest:
        return None

    completed: list[str] = []
    running: list[str] = []
    errors: list[tuple[str, str | None]] = []
    scheduled: list[tuple[str, datetime | None]] = []

    for workflow, execution in latest.items():
        name = _WORKFLOW_DISPLAY.get(workflow, workflow)
        if execution.status == ExecutionStatus.COMPLETED.value:
            completed.append(name)
        elif execution.status in _ACTIVE_STATUSES:
            running.append(name)
        elif execution.status == ExecutionStatus.SCHEDULED.value:
            scheduled.append((name, execution.retry_at))
        elif execution.status == ExecutionStatus.FAILED.value:
            errors.append((name, execution.error_detail))

    lines = [MARKER, "#### Jeanclode Status Update"]
    completed_str = " & ".join(completed) if completed else "0"
    lines.append(f":white_check_mark: **Completed**: {completed_str}")

    if errors:
        lines.append("")
        lines.append(f":warning: **Errors**: {' & '.join(name for name, _ in errors)}")
        for name, detail in errors:
            snippet = (detail or "Please check your dashboard for details.")[:200]
            lines.append(f"> **{name}**: {snippet}")

    if running:
        lines.append("")
        lines.append(f":runner: **Running**: {' & '.join(running)}")

    if scheduled:
        lines.append("")
        lines.append(
            f":hourglass_flowing_sand: **Deferred**: {' & '.join(n for n, _ in scheduled)}"
        )
        for name, retry_at in scheduled:
            when = retry_at.strftime("%Y-%m-%d %H:%M UTC") if retry_at else "soon"
            lines.append(
                f"> **{name}** — rate limited, retrying at `{when}`. "
                "Want to cancel? See your dashboard."
            )

    return "\n".join(lines)


async def sync_status_comment_for_pull_request(pr_id: UUID) -> None:
    """Upsert the sticky status comment on a PR/MR."""
    app = get_current_app()
    db_plugin = app.database
    faststream = app.faststream
    if not db_plugin or not faststream:
        return

    lock = faststream.get_redis().lock(
        f"jeanclode:status-comment:pr:{pr_id}",
        timeout=_LOCK_TIMEOUT,
        blocking_timeout=_LOCK_BLOCKING_TIMEOUT,
    )
    if not await lock.acquire():
        logger.warning("Could not acquire status-comment lock for PR %s", pr_id)
        return

    try:
        provider = owner = repo_name = installation_id = None
        project_id = encrypted_token = base_url = None
        number = 0
        body = None

        with db_plugin.session() as db:
            pr = db.query(PullRequest).filter(PullRequest.id == pr_id).first()
            if not pr:
                return
            repo = db_get_repository_by_id(db, pr.repository_id)
            if not repo or repo.provider not in ("github", "gitlab"):
                return
            org = db_get_org_by_id(db, repo.org_id)
            if not org:
                return

            latest = db_get_latest_executions_for_pr(db, pr_id, _PR_WORKFLOWS)
            body = build_status_body(latest)
            if body is None:
                return

            provider = repo.provider
            number = pr.pr_number
            if provider == "github":
                owner, _, repo_name = (repo.name or "").partition("/")
                installation_id = org.installation_id
            elif app.gitlab:
                encrypted_token, base_url = app.gitlab.resolve_repo_token(db, repo)
                project_id = repo.external_id

        if body is None:
            return

        if provider == "github":
            if not (owner and repo_name and installation_id and app.github):
                logger.warning("Missing github context for status comment on PR %s", pr_id)
                return
            token = await app.github.get_installation_access_token(installation_id)
            await upsert_github_comment(
                app.github,
                installation_token=token,
                owner=owner,
                repo=repo_name,
                number=number,
                marker=MARKER,
                body=body,
            )
        elif provider == "gitlab":
            if not (encrypted_token and project_id and app.gitlab and db_plugin):
                logger.warning("Missing gitlab context for status comment on PR %s", pr_id)
                return
            access_token = db_plugin.decrypt(encrypted_token)
            await upsert_gitlab_note(
                app.gitlab,
                access_token=access_token,
                project_id=project_id,
                resource="merge_requests",
                iid=number,
                marker=MARKER,
                body=body,
                provider_url=base_url,
            )
    except Exception:
        logger.exception("Failed to sync status comment for PR %s", pr_id)
    finally:
        await lock.release()


async def sync_status_comment_for_issue(issue_id: UUID) -> None:
    """Upsert the sticky status comment on an Issue.

    No-op for Sentry-sourced issues (``repository.provider == "sentry"``)
    since there's no git-hosted issue to comment on.
    """
    app = get_current_app()
    db_plugin = app.database
    faststream = app.faststream
    if not db_plugin or not faststream:
        return

    lock = faststream.get_redis().lock(
        f"jeanclode:status-comment:issue:{issue_id}",
        timeout=_LOCK_TIMEOUT,
        blocking_timeout=_LOCK_BLOCKING_TIMEOUT,
    )
    if not await lock.acquire():
        logger.warning("Could not acquire status-comment lock for issue %s", issue_id)
        return

    try:
        provider = owner = repo_name = installation_id = None
        project_id = encrypted_token = base_url = None
        number = 0
        body = None

        with db_plugin.session() as db:
            issue = db_get_issue_by_id(db, issue_id)
            if not issue or not issue.external_id:
                return
            repo = db_get_repository_by_id(db, issue.repository_id)
            if not repo or repo.provider not in ("github", "gitlab"):
                return
            org = db_get_org_by_id(db, repo.org_id)
            if not org:
                return

            latest = db_get_latest_executions_for_issue_targets(db, issue_id, _ISSUE_WORKFLOWS)
            body = build_status_body(latest)
            if body is None:
                return

            provider = repo.provider
            number = int(issue.external_id)
            if provider == "github":
                owner, _, repo_name = (repo.name or "").partition("/")
                installation_id = org.installation_id
            elif app.gitlab:
                encrypted_token, base_url = app.gitlab.resolve_repo_token(db, repo)
                project_id = repo.external_id

        if body is None:
            return

        if provider == "github":
            if not (owner and repo_name and installation_id and app.github):
                logger.warning("Missing github context for status comment on issue %s", issue_id)
                return
            token = await app.github.get_installation_access_token(installation_id)
            await upsert_github_comment(
                app.github,
                installation_token=token,
                owner=owner,
                repo=repo_name,
                number=number,
                marker=MARKER,
                body=body,
            )
        elif provider == "gitlab":
            if not (encrypted_token and project_id and app.gitlab and db_plugin):
                logger.warning("Missing gitlab context for status comment on issue %s", issue_id)
                return
            access_token = db_plugin.decrypt(encrypted_token)
            await upsert_gitlab_note(
                app.gitlab,
                access_token=access_token,
                project_id=project_id,
                resource="issues",
                iid=number,
                marker=MARKER,
                body=body,
                provider_url=base_url,
            )
    except Exception:
        logger.exception("Failed to sync status comment for issue %s", issue_id)
    finally:
        await lock.release()
