"""Database operations for Execution model."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import false, func, literal_column, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from api.database.organization import batch_window_minutes_clause
from api.database.repository import repo_enabled_clause
from api.models.execution_links import execution_issues, execution_pull_requests
from api.models.executions import Execution, ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.issues import Issue
from api.models.organizations import Organization
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository, RepositoryMapping
from api.models.settings import TriageTrigger


def db_create_execution(
    db: Session,
    *,
    provider: str,
    issues: Sequence[Issue] | None = None,
    pull_requests: Sequence[PullRequest] | None = None,
    workflow: str = ExecutionWorkflow.FIX.value,
    trigger: str = ExecutionTrigger.AUTO.value,
    status: str = ExecutionStatus.QUEUED.value,
    retry_target_url: str | None = None,
    prompt_text: str | None = None,
) -> Execution:
    """Create a new execution record linked to one or more targets.

    Every execution must link at least one issue or pull request (replaces the
    old ``ck_execution_has_target`` check constraint). A FIX execution typically
    links issues at creation and gains a linked pull request once it opens one.
    REVIEW/SUMMARY executions link pull requests only.

    ``provider`` is the container plugin that will run this execution
    (``github`` / ``gitlab`` / ``sentry``). It scopes reconciler queries.

    ``retry_target_url`` is set only for RESPOND executions — the mention
    comment's URL, needed to reconstruct the dispatch if this execution is
    later retried via the scheduled-redispatch path (ADR-010), since
    nothing else about the execution or its linked issue/PR can recover
    which comment triggered it.

    ``prompt_text`` is the mention/comment body itself (RESPOND only),
    surfaced read-only in the dashboard detail modal.
    """
    if not issues and not pull_requests:
        raise ValueError("Execution must link at least one issue or pull request")

    execution = Execution(
        provider=provider,
        workflow=workflow,
        trigger=trigger,
        status=status,
        retry_target_url=retry_target_url,
        prompt_text=prompt_text,
    )
    if issues:
        execution.issues = list(issues)
    if pull_requests:
        execution.pull_requests = list(pull_requests)
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


def db_get_execution_by_id(db: Session, execution_id: UUID) -> Execution | None:
    """Get execution by ID."""
    return db.query(Execution).filter(Execution.id == execution_id).first()


def db_link_execution_pull_requests(
    db: Session,
    execution_id: UUID,
    pr_ids: Sequence[UUID],
) -> None:
    """Link an execution to pull requests via ``execution_pull_requests``.

    Idempotent — rows already present are skipped, so a redelivered terminal
    status message doesn't error on the PK. This link is the sole source of
    truth for "a PR this bot opened for a fix" (Part D): the merge gate and
    the dashboard status both key off it.
    """
    if not pr_ids:
        return
    existing = set(
        db.execute(
            select(execution_pull_requests.c.pull_request_id).where(
                execution_pull_requests.c.execution_id == execution_id
            )
        ).scalars()
    )
    new_ids = [pid for pid in dict.fromkeys(pr_ids) if pid not in existing]
    if not new_ids:
        return
    db.execute(
        execution_pull_requests.insert(),
        [{"execution_id": execution_id, "pull_request_id": pid} for pid in new_ids],
    )
    db.commit()


_TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)


def db_update_execution_status(
    db: Session,
    execution_id: UUID,
    status: str,
    *,
    error_type: str | None = None,
    error_detail: str | None = None,
) -> Execution | None:
    """Update execution status and optional error fields.

    Terminal statuses (completed, failed, cancelled) are sticky — once set,
    further updates are ignored. Protects against late/duplicate status
    messages (e.g. reconcile republishing) clobbering a real outcome.
    """
    execution = db_get_execution_by_id(db, execution_id)
    if not execution:
        return None

    if execution.status in _TERMINAL_STATUSES:
        return execution

    execution.status = status

    if error_type is not None:
        execution.error_type = error_type
    if error_detail is not None:
        execution.error_detail = error_detail[:2000]

    db.commit()
    db.refresh(execution)
    return execution


def db_mark_execution_scheduled(
    db: Session,
    execution_id: UUID,
    *,
    retry_at: datetime,
) -> Execution | None:
    """Admit an execution as SCHEDULED with a ``retry_at`` (ADR-010).

    Used both at admission time (``launch_*`` found every LLM credential
    stale before ever starting a container) and by the watcher reacting to
    a mid-run 429 — both writers land on this same shape. Terminal
    statuses stay sticky, same guard as :func:`db_update_execution_status`.
    """
    execution = db_get_execution_by_id(db, execution_id)
    if not execution:
        return None

    if execution.status in _TERMINAL_STATUSES:
        return execution

    execution.status = ExecutionStatus.SCHEDULED.value
    execution.retry_at = retry_at
    db.commit()
    db.refresh(execution)
    return execution


def db_claim_due_scheduled_executions(db: Session, limit: int) -> list[Execution]:
    """Atomically claim SCHEDULED executions whose ``retry_at`` has elapsed.

    ``FOR UPDATE SKIP LOCKED`` — same idiom as ADR-006's dispatchable-issues
    query — so multiple backend pods running the redispatch poller never
    double-claim the same row. Flips status to QUEUED inside the same
    transaction the caller commits, so no other pod's tick can also claim
    it once this returns.
    """
    rows = (
        db.query(Execution)
        .filter(
            Execution.status == ExecutionStatus.SCHEDULED.value,
            Execution.retry_at.is_not(None),
            Execution.retry_at <= func.now(),
        )
        .order_by(Execution.retry_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    for execution in rows:
        execution.status = ExecutionStatus.QUEUED.value
        execution.retry_at = None
    db.commit()
    for execution in rows:
        db.refresh(execution)
    return rows


def db_cancel_scheduled_execution(db: Session, execution_id: UUID) -> bool:
    """Cancel a SCHEDULED execution via a single conditional update.

    Conditional rather than a bare write: the redispatch poller could claim
    the same row for redispatch at the same instant. If no row is updated,
    the poller won that race a moment earlier — report that as a lost race,
    not silent success.
    """
    result = db.execute(
        Execution.__table__.update()
        .where(
            Execution.id == execution_id,
            Execution.status == ExecutionStatus.SCHEDULED.value,
        )
        .values(status=ExecutionStatus.CANCELLED.value, retry_at=None)
    )
    db.commit()
    return result.rowcount > 0


def db_cancel_running_execution(db: Session, execution_id: UUID) -> bool:
    """Cancel a RUNNING execution via a single conditional update.

    RUNNING only, not QUEUED: a QUEUED execution has no container yet, and
    dispatch (consuming its own event off the stream) can lag admission by
    up to the redispatch/poll interval — cancelling during that window would
    flip the DB row to CANCELLED while the dispatch consumer, unaware, goes
    on to actually launch the container a moment later, orphaning it with
    nothing left watching to kill it. RUNNING means the container already
    exists, so there's always something for the caller's stop_container
    call to actually kill.

    Conditional rather than a bare write for the usual reason: the container
    could reach a terminal status (finish naturally) between the caller's
    container-kill call and this write. Losing that race means the real
    outcome already landed and should stand, not get overwritten by
    CANCELLED.
    """
    result = db.execute(
        Execution.__table__.update()
        .where(
            Execution.id == execution_id,
            Execution.status == ExecutionStatus.RUNNING.value,
        )
        .values(status=ExecutionStatus.CANCELLED.value)
    )
    db.commit()
    return result.rowcount > 0


def db_update_execution_container(
    db: Session,
    execution_id: UUID,
    container_id: str,
) -> Execution | None:
    """Update execution with container ID."""
    execution = db_get_execution_by_id(db, execution_id)
    if not execution:
        return None

    execution.container_id = container_id
    db.commit()
    db.refresh(execution)
    return execution


def db_has_active_execution(db: Session, issue_id: UUID, *, workflow: str | None = None) -> bool:
    """Check if an issue has a queued or running execution.

    Pass ``workflow`` to restrict the check to a specific workflow type —
    e.g. only block a second ISSUE_RESOLVE from starting, not an unrelated
    RESPOND execution that happens to reference the same issue.
    """
    q = (
        db.query(Execution.id)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .filter(
            execution_issues.c.issue_id == issue_id,
            Execution.status.in_([ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]),
        )
    )
    if workflow is not None:
        q = q.filter(Execution.workflow == workflow)
    return q.first() is not None


def db_get_next_queued_respond_execution(
    db: Session,
    *,
    pull_request_id: UUID | None = None,
    issue_id: UUID | None = None,
) -> Execution | None:
    """Oldest QUEUED respond execution waiting behind another on this PR/issue.

    Respond dispatch admits a second mention as QUEUED instead of dropping it
    when one is already active for the same PR/issue (see
    ``api.plugins.container.respond_queue``), but never publishes it — this is
    how the queue is drained once the one ahead of it clears.
    """
    query = db.query(Execution).filter(
        Execution.workflow == ExecutionWorkflow.RESPOND.value,
        Execution.status == ExecutionStatus.QUEUED.value,
    )
    if pull_request_id is not None:
        query = query.join(
            execution_pull_requests, execution_pull_requests.c.execution_id == Execution.id
        ).filter(execution_pull_requests.c.pull_request_id == pull_request_id)
    elif issue_id is not None:
        query = query.join(
            execution_issues, execution_issues.c.execution_id == Execution.id
        ).filter(execution_issues.c.issue_id == issue_id)
    else:
        return None
    return query.order_by(Execution.created_at.asc()).first()


def db_get_failed_execution_count(db: Session, issue_id: UUID) -> int:
    """Get count of failed executions for an issue (replaces retry_count)."""
    return (
        db.query(func.count())
        .select_from(Execution)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .filter(
            execution_issues.c.issue_id == issue_id,
            Execution.status == ExecutionStatus.FAILED,
        )
        .scalar()
        or 0
    )


def db_get_latest_execution_for_issue(db: Session, issue_id: UUID) -> Execution | None:
    """Get the most recent execution for an issue."""
    return (
        db.query(Execution)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .filter(execution_issues.c.issue_id == issue_id)
        .order_by(Execution.created_at.desc())
        .first()
    )


def db_get_latest_executions_for_pr(
    db: Session, pr_id: UUID, workflows: Sequence[str]
) -> dict[str, Execution]:
    """Get the most recent execution per workflow, for a pull request.

    Used to build the sticky status comment: one row per workflow (the
    latest run of that workflow against this PR), keyed by ``workflow``.
    """
    executions = (
        db.query(Execution)
        .join(execution_pull_requests, execution_pull_requests.c.execution_id == Execution.id)
        .filter(
            execution_pull_requests.c.pull_request_id == pr_id,
            Execution.workflow.in_(workflows),
        )
        .order_by(Execution.created_at.desc())
        .all()
    )
    latest: dict[str, Execution] = {}
    for execution in executions:
        latest.setdefault(execution.workflow, execution)
    return latest


def db_get_latest_executions_for_issue_targets(
    db: Session, issue_id: UUID, workflows: Sequence[str]
) -> dict[str, Execution]:
    """Get the most recent execution per workflow, for an issue.

    Symmetric to :func:`db_get_latest_executions_for_pr`, scoped to the
    ``execution_issues`` link table instead.
    """
    executions = (
        db.query(Execution)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .filter(
            execution_issues.c.issue_id == issue_id,
            Execution.workflow.in_(workflows),
        )
        .order_by(Execution.created_at.desc())
        .all()
    )
    latest: dict[str, Execution] = {}
    for execution in executions:
        latest.setdefault(execution.workflow, execution)
    return latest


# ── Watcher queries ──────────────────────────────────────────────────


def db_get_running_executions(
    db: Session,
    *,
    provider: str,
    exclude_ids: set[str] | None = None,
) -> list[Execution]:
    """Get this plugin's QUEUED/RUNNING executions.

    The ``provider`` filter scopes results to a single container plugin so
    that a github reconciler cannot mark sentry-owned executions stale and
    vice versa (#104).

    Args:
        db: Database session.
        provider: Plugin name (``github`` / ``gitlab`` / ``sentry``).
        exclude_ids: Execution IDs to exclude (those with known running containers).

    Returns:
        List of executions in active status owned by ``provider``.
    """
    query = db.query(Execution).filter(
        Execution.provider == provider,
        Execution.status.in_([ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]),
    )
    if exclude_ids:
        query = query.filter(~Execution.id.in_([UUID(eid) for eid in exclude_ids]))
    return query.all()


# ── Dispatch queries ─────────────────────────────────────────────────


def _git_org_running_fix_exists(git_org_id_col):
    """EXISTS: a ``sentry`` FIX execution is QUEUED/RUNNING for this git org.

    An execution's git org is reached the same way a dispatch target is:
    ``Execution → issue → Sentry Repository → RepositoryMapping → git
    Repository → org_id``. Manual fixes count too (same workflow + provider).
    """
    ex_issue = aliased(Issue)
    ex_sentry_repo = aliased(Repository)
    ex_map = aliased(RepositoryMapping)
    ex_git_repo = aliased(Repository)
    return (
        select(literal_column("1"))
        .select_from(Execution)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .join(ex_issue, ex_issue.id == execution_issues.c.issue_id)
        .join(ex_sentry_repo, ex_sentry_repo.id == ex_issue.repository_id)
        .join(ex_map, ex_map.repo_id == ex_sentry_repo.id)
        .join(ex_git_repo, ex_git_repo.id == ex_map.mapped_repo_id)
        .where(
            Execution.provider == "sentry",
            Execution.workflow == ExecutionWorkflow.FIX.value,
            Execution.status.in_([ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value]),
            ex_git_repo.org_id == git_org_id_col,
        )
        .exists()
    )


def db_git_org_has_running_fix_execution(db: Session, git_org_id: UUID) -> bool:
    """The git org is the perimeter: one fix batch runs per git org at a time.

    While a fix container for this git org is still working, the dispatcher
    must not start another for it — a second concurrent fixer on the same git
    org doubles that org's container and LLM load and races the first on the
    same repos. Different git orgs are unaffected and dispatch concurrently.
    Checked before the window is claimed so a long fixer doesn't burn the
    org's window while it runs.
    """
    return db.query(_git_org_running_fix_exists(git_org_id)).scalar()


def _non_failed_fix_exec_exists():
    """Correlated EXISTS: this issue has a non-FAILED FIX execution."""
    return (
        select(Execution.id)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .where(
            execution_issues.c.issue_id == Issue.id,
            Execution.workflow == ExecutionWorkflow.FIX.value,
            Execution.status != ExecutionStatus.FAILED,
        )
        .correlate(Issue)
        .exists()
    )


def _failed_fix_exec_count():
    """Correlated scalar: this issue's FAILED FIX execution count."""
    return (
        select(func.count())
        .select_from(Execution)
        .join(execution_issues, execution_issues.c.execution_id == Execution.id)
        .where(
            execution_issues.c.issue_id == Issue.id,
            Execution.workflow == ExecutionWorkflow.FIX.value,
            Execution.status == ExecutionStatus.FAILED,
        )
        .correlate(Issue)
        .scalar_subquery()
    )


def _git_org_open_fix_pr_exists(git_org_id_col):
    """Correlated EXISTS: a repo under this git org still has an open FIX-linked PR.

    This is the merge gate (Part E2): the sole marker is the definite
    ``execution_pull_requests`` link from a FIX execution — never a branch
    name. "Closed counts as done", so only ``state = 'open'`` blocks.
    """
    return (
        select(literal_column("1"))
        .select_from(PullRequest)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(
            execution_pull_requests,
            execution_pull_requests.c.pull_request_id == PullRequest.id,
        )
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .where(
            Repository.org_id == git_org_id_col,
            Execution.workflow == ExecutionWorkflow.FIX.value,
            PullRequest.state == PRState.OPEN.value,
        )
        .exists()
    )


def db_get_eligible_dispatch_targets(
    db: Session,
    provider: str,
    max_retries: int,
) -> list[tuple[UUID, UUID]]:
    """``(sentry_org_id, git_org_id)`` pairs with a dispatchable batch waiting.

    Sentry dispatch partitions by the **git org that owns the mapped target
    repo**: one Sentry org's projects can map into several git orgs, and a
    container gets exactly one git-platform token, so each git org is its own
    independent batch — its own window, its own merge gate, dispatched
    concurrently with the others.

    A pair is returned when, joining
    ``Issue → Repository(sentry) → RepositoryMapping → Repository(git)``:

    - it has ≥1 dispatchable issue: mapped + enabled git target, no non-FAILED
      FIX execution, fewer than ``max_retries`` failed FIX executions
    - the **git** org's ``last_dispatched_at`` is NULL or older than the
      **Sentry** org's ``batch_window`` ago
    - the Sentry org's ``triggers.triage`` is ``automatic``
    - the git org has no fix batch currently running (the git org is the
      concurrency perimeter — one batch per git org at a time)
    - when the Sentry org enabled ``gate_on_open_fix_prs``: no repo under that
      git org has an open FIX-linked PR (Part E2)
    """
    sentry_org = aliased(Organization)
    git_org = aliased(Organization)
    sentry_repo = aliased(Repository)
    git_repo = aliased(Repository)

    gate_enabled = func.coalesce(sentry_org.settings["gate_on_open_fix_prs"].as_boolean(), false())

    rows = (
        db.query(sentry_org.id, git_org.id)
        .select_from(Issue)
        .join(sentry_repo, Issue.repository_id == sentry_repo.id)
        .join(sentry_org, sentry_repo.org_id == sentry_org.id)
        .join(RepositoryMapping, RepositoryMapping.repo_id == sentry_repo.id)
        .join(git_repo, RepositoryMapping.mapped_repo_id == git_repo.id)
        .join(git_org, git_repo.org_id == git_org.id)
        .filter(
            sentry_org.provider == provider,
            sentry_org.settings["triggers"]["triage"].as_string() == TriageTrigger.AUTOMATIC.value,
            RepositoryMapping.mapped_repo_id.is_not(None),
            repo_enabled_clause(git_repo),
            ~_non_failed_fix_exec_exists(),
            _failed_fix_exec_count() < max_retries,
            (git_org.last_dispatched_at.is_(None))
            | (
                git_org.last_dispatched_at
                < func.now()
                - func.make_interval(0, 0, 0, 0, 0, batch_window_minutes_clause(sentry_org), 0)
            ),
            ~_git_org_running_fix_exists(git_org.id),
            or_(~gate_enabled, ~_git_org_open_fix_pr_exists(git_org.id)),
        )
        .group_by(sentry_org.id, git_org.id)
        .all()
    )
    return [(row[0], row[1]) for row in rows]


def db_get_dispatchable_issues(
    db: Session,
    sentry_org_id: UUID,
    limit: int,
    max_retries: int,
    *,
    git_org_id: UUID | None = None,
) -> list[Issue]:
    """Get a batch of dispatchable issues for a Sentry org.

    An issue is dispatchable if:
    - Its repository is mapped to a git target (``mapped_repo_id`` is set)
    - That git target is enabled — disabling a repo on the integrations page
      stops Sentry-driven fixes landing in it, not just the git triggers
    - When ``git_org_id`` is given, that git target belongs to it — the batch
      is scoped to a single ``(sentry_org, git_org)`` partition so every issue
      in it shares one git-platform token
    - It has no execution, OR every execution is in FAILED status
    - Count of 'failed' executions < max_retries

    Uses FOR UPDATE SKIP LOCKED for row-level locking.
    """
    mapped_repo = aliased(Repository)

    filters = [
        Repository.org_id == sentry_org_id,
        RepositoryMapping.mapped_repo_id.is_not(None),
        repo_enabled_clause(mapped_repo),
        ~_non_failed_fix_exec_exists(),
        _failed_fix_exec_count() < max_retries,
    ]
    if git_org_id is not None:
        filters.append(mapped_repo.org_id == git_org_id)

    return (
        db.query(Issue)
        .join(Repository, Issue.repository_id == Repository.id)
        .join(RepositoryMapping, RepositoryMapping.repo_id == Repository.id)
        .join(mapped_repo, RepositoryMapping.mapped_repo_id == mapped_repo.id)
        .options(
            joinedload(Issue.repository)
            .joinedload(Repository.mapping)
            .joinedload(RepositoryMapping.mapped_repo),
        )
        .filter(*filters)
        .order_by(Issue.created_at)
        .limit(limit)
        .with_for_update(of=Issue, skip_locked=True)
        .all()
    )
