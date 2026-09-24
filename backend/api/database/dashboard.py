"""Database operations for dashboard endpoints."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from api.database.organization import db_get_org_subtree_ids
from api.database.repository import repo_enabled_clause
from api.models.execution_links import (
    execution_issues,
    execution_pull_requests,
    issue_pull_requests,
)
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.identities import ProviderIdentity
from api.models.issues import Issue, TriageResult
from api.models.organizations import Organization
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository, RepositoryMapping

# Friendly execution-status filter buckets -> raw ``Execution.status`` values.
# "pending" has no literal column value — it means no execution row exists
# yet (NULL after the outer join) — so it's handled separately from this map.
# "running" also matches "queued" so the filter lines up with how the UI
# already groups those two as "in progress" (see isProcessing/isReviewProcessing
# in the frontend).
_EXEC_STATUS_ALIASES: dict[str, list[str]] = {
    "running": ["queued", "running"],
    "completed": ["completed"],
    "failed": ["failed"],
}


def _execution_status_filter(column, values: list[str]):
    """Build an OR condition matching a raw exec-status column against friendly buckets.

    ``values`` come from the API's ``execution_status`` filter param (e.g.
    ``["pending", "running"]``). Returns ``None`` if nothing to filter on.
    """
    conditions = []
    literal_values: set[str] = set()
    for value in values:
        if value == "pending":
            conditions.append(column.is_(None))
        else:
            literal_values.update(_EXEC_STATUS_ALIASES.get(value, [value]))
    if literal_values:
        conditions.append(column.in_(literal_values))
    return or_(*conditions) if conditions else None


def _has_mapping_filter():
    """SQL condition mirroring ``issue_to_response``'s ``has_mapping`` field.

    Git-native repos (github/gitlab) are always considered mapped since
    they're their own repo; anything else needs an actual cross-source
    ``RepositoryMapping`` row pointing at a git repo.
    """
    return or_(
        Organization.provider.in_(["github", "gitlab"]),
        exists(
            select(RepositoryMapping.id).where(
                RepositoryMapping.repo_id == Repository.id,
                RepositoryMapping.mapped_repo_id.isnot(None),
            )
        ),
    )


def _latest_execution_subquery():
    """Subquery returning the latest execution per issue (by created_at desc).

    Joins ``execution_issues`` to get one row per (execution, issue) pair, then
    keeps only the newest execution per issue via DISTINCT ON. The PR side is
    pulled via a separate left join on ``execution_pull_requests``; for FIX
    runs there is at most one PR linked, but the schema permits more.
    """
    return (
        select(
            execution_issues.c.issue_id.label("issue_id"),
            Execution.id.label("exec_id"),
            Execution.status.label("exec_status"),
            Execution.workflow.label("exec_workflow"),
            execution_pull_requests.c.pull_request_id.label("pull_request_id"),
        )
        .select_from(execution_issues)
        .join(Execution, Execution.id == execution_issues.c.execution_id)
        .outerjoin(
            execution_pull_requests,
            execution_pull_requests.c.execution_id == Execution.id,
        )
        .distinct(execution_issues.c.issue_id)
        .order_by(execution_issues.c.issue_id, Execution.created_at.desc())
        .subquery("latest_exec")
    )


def compute_display_status(
    exec_status: str | None,
    pr_state: str | None,
    triage_result: str | None,
) -> str:
    """Compute the display status from execution + PR + triage state.

    Precedence (mirrored exactly by the SQL CASE built in
    :func:`_computed_status_expression` — keep the two in sync):
    running/queued/failed execution state wins outright; a linked PR's state
    wins over a bare "completed"; a completed-but-not-actionable triage wins
    over the generic "completed" catch-all; a triage-only not_actionable
    (no execution at all, e.g. manual dismiss) is the last resort before
    "pending".
    """
    if exec_status == ExecutionStatus.RUNNING:
        return "running"
    if exec_status == ExecutionStatus.QUEUED:
        return "pending"
    if exec_status == ExecutionStatus.FAILED:
        return "failed"
    if exec_status == ExecutionStatus.COMPLETED:
        if pr_state == "merged":
            return "pr_merged"
        if pr_state == "open":
            return "pr_open"
        if pr_state == "closed":
            return "rejected"
        if triage_result == TriageResult.NOT_ACTIONABLE:
            return "not_actionable"
        return "completed"
    if triage_result == TriageResult.NOT_ACTIONABLE:
        return "not_actionable"
    return "pending"


def _computed_status_expression(latest_exec, pr_table=None):
    """Build a CASE expression that computes the display status from triage + execution + PR state.

    Condition order must mirror :func:`compute_display_status` exactly — SQL
    can't call the Python function directly, so keep the precedence in sync
    by hand if either changes.

    Returns a column expression usable in queries.
    """
    conditions = [
        (latest_exec.c.exec_status == ExecutionStatus.RUNNING, literal("running")),
        (latest_exec.c.exec_status == ExecutionStatus.QUEUED, literal("pending")),
        (latest_exec.c.exec_status == ExecutionStatus.FAILED, literal("failed")),
    ]

    if pr_table is not None:
        conditions.extend(
            [
                (
                    (latest_exec.c.exec_status == ExecutionStatus.COMPLETED)
                    & (pr_table.c.state == "merged"),
                    literal("pr_merged"),
                ),
                (
                    (latest_exec.c.exec_status == ExecutionStatus.COMPLETED)
                    & (pr_table.c.state == "open"),
                    literal("pr_open"),
                ),
                (
                    (latest_exec.c.exec_status == ExecutionStatus.COMPLETED)
                    & (pr_table.c.state == "closed"),
                    literal("rejected"),
                ),
                (
                    (latest_exec.c.exec_status == ExecutionStatus.COMPLETED)
                    & (Issue.triage_result == TriageResult.NOT_ACTIONABLE),
                    literal("not_actionable"),
                ),
                (
                    latest_exec.c.exec_status == ExecutionStatus.COMPLETED,
                    literal("completed"),
                ),
            ]
        )
    else:
        conditions.extend(
            [
                (
                    (latest_exec.c.exec_status == ExecutionStatus.COMPLETED)
                    & (Issue.triage_result == TriageResult.NOT_ACTIONABLE),
                    literal("not_actionable"),
                ),
                (
                    latest_exec.c.exec_status == ExecutionStatus.COMPLETED,
                    literal("completed"),
                ),
            ]
        )

    conditions.append(
        (Issue.triage_result == TriageResult.NOT_ACTIONABLE, literal("not_actionable")),
    )

    return case(*conditions, else_=literal("pending"))


def db_get_issues_paginated(
    db: Session,
    workspace_id: UUID,
    *,
    page: int = 1,
    limit: int = 25,
    status: list[str] | None = None,
    search: str | None = None,
    source_org_id: UUID | None = None,
    repository_id: UUID | None = None,
    author: str | None = None,
    since: datetime | None = None,
    mapped_only: bool = False,
    execution_status: list[str] | None = None,
) -> tuple[list[tuple[Issue, str, str | None, str | None, UUID | None]], int]:
    """Get paginated issues for a workspace with computed status.

    Returns:
        Tuple of (list of (issue, computed_status, workflow, raw_exec_status,
        exec_id) tuples, total count).
    """
    latest_exec = _latest_execution_subquery()

    computed_status = _computed_status_expression(latest_exec, PullRequest.__table__)

    query = (
        db.query(
            Issue,
            computed_status.label("computed_status"),
            latest_exec.c.exec_workflow,
            latest_exec.c.exec_status,
            latest_exec.c.exec_id,
        )
        .join(Repository, Issue.repository_id == Repository.id)
        .join(Organization, Repository.org_id == Organization.id)
        .outerjoin(latest_exec, latest_exec.c.issue_id == Issue.id)
        .outerjoin(PullRequest, latest_exec.c.pull_request_id == PullRequest.id)
        .options(
            joinedload(Issue.repository).joinedload(Repository.organization),
        )
        .filter(Organization.workspace_id == workspace_id)
    )

    if status:
        # Map universal open/closed to source-specific statuses
        open_statuses = ["open", "unresolved"]
        closed_statuses = ["closed", "resolved"]
        filter_values = []
        for s in status:
            if s == "open":
                filter_values.extend(open_statuses)
            elif s == "closed":
                filter_values.extend(closed_statuses)
            else:
                filter_values.append(s)
        query = query.filter(Issue.status.in_(filter_values))

    if search:
        query = query.filter(Issue.title.ilike(f"%{search}%"))

    if source_org_id:
        query = query.filter(
            Organization.id.in_(db_get_org_subtree_ids(db, [source_org_id], workspace_id))
        )

    if repository_id:
        query = query.filter(Repository.id == repository_id)

    if author:
        query = query.filter(Issue.author == author)

    if since:
        query = query.filter(
            func.coalesce(Issue.last_seen, Issue.first_seen, Issue.created_at) >= since
        )

    if mapped_only:
        query = query.filter(_has_mapping_filter())

    if execution_status:
        exec_status_condition = _execution_status_filter(
            latest_exec.c.exec_status, execution_status
        )
        if exec_status_condition is not None:
            query = query.filter(exec_status_condition)

    total = query.count()
    offset = (page - 1) * limit
    results = (
        query.order_by(func.coalesce(Issue.first_seen, Issue.created_at).desc(), Issue.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return results, total


def db_get_issue_detail(
    db: Session,
    issue_id: UUID,
) -> Issue | None:
    """Get a single issue with repository and executions eagerly loaded."""
    return (
        db.query(Issue)
        .options(
            joinedload(Issue.repository)
            .joinedload(Repository.mapping)
            .joinedload(RepositoryMapping.mapped_repo),
            selectinload(Issue.executions).selectinload(Execution.pull_requests),
            selectinload(Issue.pull_requests),
        )
        .filter(Issue.id == issue_id)
        .first()
    )


def db_get_workspace_pull_requests(
    db: Session,
    workspace_id: UUID,
    *,
    page: int = 1,
    limit: int = 25,
    status: str | None = None,
    search: str | None = None,
    org_id: UUID | None = None,
    repository_id: UUID | None = None,
    author: str | None = None,
    since: datetime | None = None,
    execution_status: list[str] | None = None,
) -> tuple[list, int]:
    """Get paginated pull requests for a workspace.

    Scopes via PullRequest -> Repository -> Organization -> workspace_id.
    Optionally filters by org_id, repository_id, author, and joins latest
    Execution for execution_status.

    Returns:
        Tuple of (list of (PullRequest, exec_status_columns...) rows, total count).
    """
    # Latest REVIEW execution per PR (DISTINCT ON), via the M2M link
    # table. Summary runs are intentionally excluded — the row badge
    # reflects review state only, matching the predecessor project's convention. If
    # summary state ever needs to surface separately, add a parallel
    # subquery rather than mixing workflows here.
    latest_exec = (
        select(
            execution_pull_requests.c.pull_request_id.label("exec_pr_id"),
            Execution.id.label("exec_id"),
            Execution.status.label("exec_status"),
            Execution.workflow.label("exec_workflow"),
            Execution.error_type.label("exec_error_type"),
            Execution.error_detail.label("exec_error_detail"),
            Execution.trigger.label("exec_trigger"),
            Execution.created_at.label("exec_created_at"),
        )
        .select_from(execution_pull_requests)
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .where(Execution.workflow == ExecutionWorkflow.REVIEW.value)
        .distinct(execution_pull_requests.c.pull_request_id)
        .order_by(execution_pull_requests.c.pull_request_id, Execution.created_at.desc())
        .subquery("latest_exec")
    )

    query = (
        db.query(
            PullRequest,
            latest_exec.c.exec_id,
            latest_exec.c.exec_status,
            latest_exec.c.exec_workflow,
            latest_exec.c.exec_error_type,
            latest_exec.c.exec_error_detail,
            latest_exec.c.exec_trigger,
            latest_exec.c.exec_created_at,
            Repository.provider.label("repo_provider"),
        )
        .join(Repository, PullRequest.repository_id == Repository.id)
        .join(Organization, Repository.org_id == Organization.id)
        .filter(Organization.workspace_id == workspace_id)
        .outerjoin(latest_exec, latest_exec.c.exec_pr_id == PullRequest.id)
    )

    if status:
        query = query.filter(PullRequest.state == status)

    if org_id:
        query = query.filter(
            Organization.id.in_(db_get_org_subtree_ids(db, [org_id], workspace_id))
        )

    if repository_id:
        query = query.filter(PullRequest.repository_id == repository_id)

    if author:
        query = query.filter(PullRequest.author == author)

    if search:
        query = query.filter(PullRequest.title.ilike(f"%{search}%"))

    if since:
        query = query.filter(PullRequest.created_at >= since)

    if execution_status:
        exec_status_condition = _execution_status_filter(
            latest_exec.c.exec_status, execution_status
        )
        if exec_status_condition is not None:
            query = query.filter(exec_status_condition)

    total = query.count()
    offset = (page - 1) * limit
    results = query.order_by(PullRequest.created_at.desc()).offset(offset).limit(limit).all()

    return results, total


def _execution_in_workspace(workspace_id: UUID):
    """Condition: the execution links an issue or a PR belonging to ``workspace_id``.

    Fix/resolve runs reach the workspace through their issues, review/summary
    runs through their PRs. Two correlated EXISTS rather than outer joins, so a
    batched run linking N issues matches once and no DISTINCT is needed.
    """
    via_issue = (
        select(literal(1))
        .select_from(execution_issues)
        .join(Issue, Issue.id == execution_issues.c.issue_id)
        .join(Repository, Repository.id == Issue.repository_id)
        .join(Organization, Organization.id == Repository.org_id)
        .where(
            execution_issues.c.execution_id == Execution.id,
            Organization.workspace_id == workspace_id,
        )
        .exists()
    )
    via_pull_request = (
        select(literal(1))
        .select_from(execution_pull_requests)
        .join(PullRequest, PullRequest.id == execution_pull_requests.c.pull_request_id)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(Organization, Organization.id == Repository.org_id)
        .where(
            execution_pull_requests.c.execution_id == Execution.id,
            Organization.workspace_id == workspace_id,
        )
        .exists()
    )
    return or_(via_issue, via_pull_request)


def _execution_card_options():
    return (
        selectinload(Execution.issues)
        .joinedload(Issue.repository)
        .joinedload(Repository.organization),
        selectinload(Execution.pull_requests)
        .joinedload(PullRequest.repository)
        .joinedload(Repository.organization),
    )


def db_get_workspace_executions(
    db: Session,
    workspace_id: UUID,
    *,
    status: str | None = None,
    page: int = 1,
    limit: int = 12,
) -> tuple[list[Execution], int]:
    """Get paginated executions for a workspace.

    Returns:
        Tuple of (list of Execution objects, total count).
    """
    query = db.query(Execution).filter(_execution_in_workspace(workspace_id))
    if status:
        query = query.filter(Execution.status == status)

    total = query.count()

    offset = (page - 1) * limit
    results = (
        query.options(*_execution_card_options())
        .order_by(Execution.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return results, total


# What the dashboard's active-executions panel shows, newest first per status.
ACTIVE_EXECUTION_LIMITS: dict[str, int] = {
    ExecutionStatus.RUNNING.value: 50,
    ExecutionStatus.QUEUED.value: 20,
}


def db_get_workspace_active_executions(db: Session, workspace_id: UUID) -> list[Execution]:
    """Running then queued executions of a workspace, each capped per ``ACTIVE_EXECUTION_LIMITS``.

    One query and no COUNT: the dashboard used to make a paginated request per
    status and never read the totals.
    """
    rank = (
        func.row_number()
        .over(partition_by=Execution.status, order_by=Execution.created_at.desc())
        .label("rank")
    )
    ranked = (
        select(Execution.id.label("id"), Execution.status.label("status"), rank)
        .where(
            Execution.status.in_(list(ACTIVE_EXECUTION_LIMITS)),
            _execution_in_workspace(workspace_id),
        )
        .subquery("ranked")
    )
    within_limit = or_(
        *(
            and_(ranked.c.status == status, ranked.c.rank <= limit)
            for status, limit in ACTIVE_EXECUTION_LIMITS.items()
        )
    )
    return (
        db.query(Execution)
        .join(ranked, ranked.c.id == Execution.id)
        .filter(within_limit)
        .options(*_execution_card_options())
        .order_by(
            case((Execution.status == ExecutionStatus.RUNNING.value, 0), else_=1),
            Execution.created_at.desc(),
        )
        .all()
    )


STATS_WINDOW_DAYS = 30
TOP_USERS_LIMIT = 2


def _workspace_pr_ids(workspace_id: UUID):
    return (
        select(PullRequest.id)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(Organization, Organization.id == Repository.org_id)
        .where(Organization.workspace_id == workspace_id)
    )


def _completed_prs(workflow: ExecutionWorkflow, since: datetime | None = None):
    """PR ids with at least one completed run of ``workflow``."""
    conditions = [
        Execution.workflow == workflow.value,
        Execution.status == ExecutionStatus.COMPLETED.value,
    ]
    if since is not None:
        conditions.append(Execution.created_at >= since)
    return (
        select(execution_pull_requests.c.pull_request_id)
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .where(*conditions)
    )


def _count_workspace_prs(db: Session, workspace_id: UUID, *conditions) -> int:
    return (
        db.query(func.count(PullRequest.id))
        .filter(PullRequest.id.in_(_workspace_pr_ids(workspace_id)), *conditions)
        .scalar()
        or 0
    )


def db_get_workspace_stats(db: Session, workspace_id: UUID) -> dict:
    """Every stat card in the dashboard, issues and PR pages.

    Runs, reviews and pings are counted over the last ``STATS_WINDOW_DAYS``;
    the issue and PR page cards are snapshots, matching the tables under them.
    """
    since = datetime.now(UTC) - timedelta(days=STATS_WINDOW_DAYS)
    in_workspace = _execution_in_workspace(workspace_id)

    running, queued, successful_runs = (
        db.query(
            func.count().filter(Execution.status == ExecutionStatus.RUNNING.value),
            func.count().filter(Execution.status == ExecutionStatus.QUEUED.value),
            func.count().filter(
                Execution.status == ExecutionStatus.COMPLETED.value,
                Execution.created_at >= since,
            ),
        )
        .select_from(Execution)
        .filter(
            or_(
                Execution.status.in_([ExecutionStatus.RUNNING.value, ExecutionStatus.QUEUED.value]),
                Execution.created_at >= since,
            ),
            in_workspace,
        )
        .one()
    )

    top_users = (
        db.query(ProviderIdentity, func.count(Execution.id).label("pings"))
        .join(Execution, Execution.triggered_by_identity_id == ProviderIdentity.id)
        .filter(
            Execution.workflow == ExecutionWorkflow.RESPOND.value,
            Execution.created_at >= since,
            in_workspace,
        )
        .group_by(ProviderIdentity.id)
        .order_by(func.count(Execution.id).desc(), ProviderIdentity.username)
        .limit(TOP_USERS_LIMIT)
        .all()
    )

    workspace_issue_ids = (
        select(Issue.id)
        .join(Repository, Repository.id == Issue.repository_id)
        .join(Organization, Organization.id == Repository.org_id)
        .where(Organization.workspace_id == workspace_id)
    )
    has_fix_pr = exists(
        select(issue_pull_requests.c.issue_id).where(issue_pull_requests.c.issue_id == Issue.id)
    )
    issues_handled = (
        db.query(func.count(Issue.id))
        .filter(
            Issue.id.in_(workspace_issue_ids),
            or_(Issue.triage_result == TriageResult.NOT_ACTIONABLE.value, has_fix_pr),
        )
        .scalar()
        or 0
    )
    fix_pr_ids = select(issue_pull_requests.c.pull_request_id).where(
        issue_pull_requests.c.issue_id.in_(workspace_issue_ids)
    )
    fix_prs_created, fix_prs_merged = (
        db.query(
            func.count(PullRequest.id),
            func.count(PullRequest.id).filter(PullRequest.state == PRState.MERGED.value),
        )
        .filter(PullRequest.id.in_(fix_pr_ids))
        .one()
    )

    reviewed = _completed_prs(ExecutionWorkflow.REVIEW)
    pending_review = (
        db.query(func.count(PullRequest.id))
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(Organization, Organization.id == Repository.org_id)
        .filter(
            Organization.workspace_id == workspace_id,
            PullRequest.state == PRState.OPEN.value,
            repo_enabled_clause(),
            PullRequest.id.notin_(reviewed),
        )
        .scalar()
        or 0
    )

    return {
        "window_days": STATS_WINDOW_DAYS,
        "dashboard": {
            "running": running,
            "queued": queued,
            "successful_runs": successful_runs,
            "reviewed_prs": _count_workspace_prs(
                db,
                workspace_id,
                PullRequest.id.in_(_completed_prs(ExecutionWorkflow.REVIEW, since)),
            ),
            "top_users": [
                {
                    "identity_id": identity.id,
                    "username": identity.username,
                    "avatar_url": identity.avatar_url,
                    "provider": identity.provider,
                    "pings": pings,
                }
                for identity, pings in top_users
            ],
        },
        "issues": {
            "handled": issues_handled,
            "prs_created": fix_prs_created,
            "prs_merged": fix_prs_merged,
        },
        "pull_requests": {
            "reviewed": _count_workspace_prs(db, workspace_id, PullRequest.id.in_(reviewed)),
            "pending_review": pending_review,
            "summarized": _count_workspace_prs(
                db, workspace_id, PullRequest.id.in_(_completed_prs(ExecutionWorkflow.SUMMARY))
            ),
        },
    }


def db_get_org_stats(
    db: Session,
    org_id: UUID,
    provider: str,
) -> dict[str, int | float | str | None]:
    """Get per-organization stats for the dashboard.

    For sentry orgs: issue counts and fix execution stats.
    For git orgs: repo/PR counts and review execution stats.
    Also returns latest activity (most recent completed execution with PR).

    Counts cover the org's whole subtree. A GitLab group is connected once and
    the integrations page shows it as one card, but its projects are spread
    across an org per subgroup namespace — counting only the rows hanging off
    the group itself reports a fraction of what the tenant connected.
    """
    org = db.query(Organization).filter(Organization.id == org_id).first()
    org_scope = (
        db_get_org_subtree_ids(db, [org_id], org.workspace_id)
        if org is not None and org.workspace_id is not None
        else [org_id]
    )

    result: dict[str, int | float | str | None] = {
        "total_issues": 0,
        "issues_fixing": 0,
        "issues_fixed": 0,
        "issues_failed": 0,
        "fix_success_rate": 0.0,
        "repo_count": 0,
        "total_prs": 0,
        "prs_reviewed": 0,
        "prs_open": 0,
        "latest_title": None,
        "latest_pr_url": None,
        "latest_pr_number": None,
        "latest_at": None,
    }

    if provider == "sentry":
        # Issue counts
        total_issues = (
            db.query(func.count())
            .select_from(Issue)
            .join(Repository, Issue.repository_id == Repository.id)
            .filter(Repository.org_id.in_(org_scope))
            .scalar()
            or 0
        )
        result["total_issues"] = total_issues

        # Fix execution counts — count distinct issues per status, since one
        # batched FIX execution covers N issues but they each get a status row.
        fix_stats = (
            db.query(
                Execution.status,
                func.count(func.distinct(execution_issues.c.issue_id)),
            )
            .select_from(Execution)
            .join(execution_issues, execution_issues.c.execution_id == Execution.id)
            .join(Issue, Issue.id == execution_issues.c.issue_id)
            .join(Repository, Issue.repository_id == Repository.id)
            .filter(
                Repository.org_id.in_(org_scope),
                Execution.workflow == ExecutionWorkflow.FIX,
            )
            .group_by(Execution.status)
            .all()
        )
        fix_counts: dict[str, int] = {}
        for status_val, cnt in fix_stats:
            fix_counts[status_val] = cnt

        running = fix_counts.get(ExecutionStatus.RUNNING, 0) + fix_counts.get(
            ExecutionStatus.QUEUED, 0
        )
        completed = fix_counts.get(ExecutionStatus.COMPLETED, 0)
        failed = fix_counts.get(ExecutionStatus.FAILED, 0)

        result["issues_fixing"] = running
        result["issues_fixed"] = completed
        result["issues_failed"] = failed
        denominator = completed + failed
        result["fix_success_rate"] = round(completed / denominator, 4) if denominator > 0 else 0.0

        # Latest completed fix with PR
        latest_fix = (
            db.query(Issue.title, PullRequest.pr_url, PullRequest.pr_number, Execution.updated_at)
            .select_from(Execution)
            .join(execution_issues, execution_issues.c.execution_id == Execution.id)
            .join(Issue, Issue.id == execution_issues.c.issue_id)
            .join(Repository, Issue.repository_id == Repository.id)
            .join(execution_pull_requests, execution_pull_requests.c.execution_id == Execution.id)
            .join(PullRequest, PullRequest.id == execution_pull_requests.c.pull_request_id)
            .filter(
                Repository.org_id.in_(org_scope),
                Execution.workflow == ExecutionWorkflow.FIX,
                Execution.status == ExecutionStatus.COMPLETED,
            )
            .order_by(Execution.updated_at.desc())
            .first()
        )
        if latest_fix:
            result["latest_title"] = latest_fix[0]
            result["latest_pr_url"] = latest_fix[1]
            result["latest_pr_number"] = latest_fix[2]
            result["latest_at"] = latest_fix[3].isoformat() if latest_fix[3] else None

    else:
        # Git org: repos, PRs, reviews
        repo_count = (
            db.query(func.count())
            .select_from(Repository)
            .filter(Repository.org_id.in_(org_scope))
            .scalar()
            or 0
        )
        result["repo_count"] = repo_count

        # PR counts
        pr_stats = (
            db.query(PullRequest.state, func.count())
            .join(Repository, PullRequest.repository_id == Repository.id)
            .filter(Repository.org_id.in_(org_scope))
            .group_by(PullRequest.state)
            .all()
        )
        total_prs = 0
        prs_open = 0
        for state, cnt in pr_stats:
            total_prs += cnt
            if state == "open":
                prs_open = cnt
        result["total_prs"] = total_prs
        result["prs_open"] = prs_open

        # Review executions completed — distinct PRs to absorb batched review runs.
        prs_reviewed = (
            db.query(func.count(func.distinct(execution_pull_requests.c.pull_request_id)))
            .select_from(Execution)
            .join(
                execution_pull_requests,
                execution_pull_requests.c.execution_id == Execution.id,
            )
            .join(PullRequest, PullRequest.id == execution_pull_requests.c.pull_request_id)
            .join(Repository, PullRequest.repository_id == Repository.id)
            .filter(
                Repository.org_id.in_(org_scope),
                Execution.workflow == ExecutionWorkflow.REVIEW,
                Execution.status == ExecutionStatus.COMPLETED,
            )
            .scalar()
            or 0
        )
        result["prs_reviewed"] = prs_reviewed

        # Latest completed review
        latest_review = (
            db.query(
                PullRequest.title,
                PullRequest.pr_url,
                PullRequest.pr_number,
                Execution.updated_at,
            )
            .select_from(Execution)
            .join(
                execution_pull_requests,
                execution_pull_requests.c.execution_id == Execution.id,
            )
            .join(PullRequest, PullRequest.id == execution_pull_requests.c.pull_request_id)
            .join(Repository, PullRequest.repository_id == Repository.id)
            .filter(
                Repository.org_id.in_(org_scope),
                Execution.workflow == ExecutionWorkflow.REVIEW,
                Execution.status == ExecutionStatus.COMPLETED,
            )
            .order_by(Execution.updated_at.desc())
            .first()
        )
        if latest_review:
            result["latest_title"] = latest_review[0]
            result["latest_pr_url"] = latest_review[1]
            result["latest_pr_number"] = latest_review[2]
            result["latest_at"] = latest_review[3].isoformat() if latest_review[3] else None

    return result


def db_get_lgtm_leaderboard(
    db: Session,
    workspace_id: UUID,
    limit: int = 10,
) -> list[dict[str, str | int | None]]:
    """Get leaderboard of PR authors with most completed bot reviews.

    Counts completed review executions grouped by PR author.
    Resolves avatar via ProviderIdentity username match.
    """
    rows = (
        db.query(
            PullRequest.author,
            func.count().label("lgtm_count"),
        )
        .join(
            execution_pull_requests,
            execution_pull_requests.c.pull_request_id == PullRequest.id,
        )
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .join(Repository, PullRequest.repository_id == Repository.id)
        .join(Organization, Repository.org_id == Organization.id)
        .filter(
            Organization.workspace_id == workspace_id,
            Execution.workflow == ExecutionWorkflow.REVIEW,
            Execution.status == ExecutionStatus.COMPLETED,
        )
        .group_by(PullRequest.author)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )

    entries = []
    for username, count in rows:
        # Try to resolve avatar from provider identity
        identity = db.query(ProviderIdentity).filter(ProviderIdentity.username == username).first()
        entries.append(
            {
                "username": username,
                "avatar_url": identity.avatar_url if identity else None,
                "lgtm_count": count,
            }
        )

    return entries


def db_get_top_pr_authors(
    db: Session,
    workspace_id: UUID,
    limit: int = 10,
) -> list[dict[str, str | int | None]]:
    """Get PR authors with the most pull requests across all repos.

    Excludes bot authors (containing '[bot]').
    """
    rows = (
        db.query(
            PullRequest.author,
            func.count().label("pr_count"),
        )
        .join(Repository, PullRequest.repository_id == Repository.id)
        .join(Organization, Repository.org_id == Organization.id)
        .filter(
            Organization.workspace_id == workspace_id,
            ~PullRequest.author.contains("[bot]"),
        )
        .group_by(PullRequest.author)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )

    entries = []
    for username, count in rows:
        identity = db.query(ProviderIdentity).filter(ProviderIdentity.username == username).first()
        entries.append(
            {
                "username": username,
                "avatar_url": identity.avatar_url if identity else None,
                "pr_count": count,
            }
        )

    return entries


def db_list_issue_repositories(
    db: Session,
    workspace_id: UUID,
    *,
    source_org_id: UUID | None = None,
    search: str | None = None,
    page: int = 1,
    limit: int = 25,
) -> tuple[list[tuple[Repository, str]], int]:
    """One page of distinct repositories that have at least one issue in the workspace.

    Used to populate the repo filter dropdown on the issues list. Returns
    (repository, org_name) pairs — the org name lets the frontend disambiguate
    Repository rows sharing a display name (e.g. a Sentry project and its
    mapped git repo, which are stored as two separate rows under two separate
    orgs, see ``Repository.mapped_repo_id``). ``search`` is a case-insensitive
    substring match on the repo name, applied server-side so the dropdown
    stays responsive for workspaces with many repos.
    """
    query = (
        db.query(Repository, Organization.name)
        .join(Organization, Repository.org_id == Organization.id)
        .join(Issue, Issue.repository_id == Repository.id)
        .filter(Organization.workspace_id == workspace_id)
        .distinct()
    )

    if source_org_id is not None:
        query = query.filter(
            Organization.id.in_(db_get_org_subtree_ids(db, [source_org_id], workspace_id))
        )

    if search and search.strip():
        query = query.filter(Repository.name.ilike(f"%{search.strip()}%"))

    total = query.count()
    rows = query.order_by(Repository.name).offset((page - 1) * limit).limit(limit).all()
    return rows, total


def db_list_pr_repositories(
    db: Session,
    workspace_id: UUID,
    *,
    org_id: UUID | None = None,
    search: str | None = None,
    page: int = 1,
    limit: int = 25,
) -> tuple[list[tuple[Repository, str]], int]:
    """One page of distinct repositories that have at least one pull request in the workspace.

    Mirrors ``db_list_issue_repositories`` for the pull requests list's repo
    filter dropdown. Returns (repository, org_name) pairs.
    """
    query = (
        db.query(Repository, Organization.name)
        .join(Organization, Repository.org_id == Organization.id)
        .join(PullRequest, PullRequest.repository_id == Repository.id)
        .filter(Organization.workspace_id == workspace_id)
        .distinct()
    )

    if org_id is not None:
        query = query.filter(
            Organization.id.in_(db_get_org_subtree_ids(db, [org_id], workspace_id))
        )

    if search and search.strip():
        query = query.filter(Repository.name.ilike(f"%{search.strip()}%"))

    total = query.count()
    rows = query.order_by(Repository.name).offset((page - 1) * limit).limit(limit).all()
    return rows, total


def db_get_active_repos(
    db: Session,
    workspace_id: UUID,
    limit: int = 10,
) -> list[dict[str, str | int | None]]:
    """Get repositories with the most agent activity.

    Counts all completed executions per repository.
    """
    rows = (
        db.query(
            Repository.name,
            Organization.provider,
            func.count().label("exec_count"),
        )
        .join(PullRequest, PullRequest.repository_id == Repository.id)
        .join(
            execution_pull_requests,
            execution_pull_requests.c.pull_request_id == PullRequest.id,
        )
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .join(Organization, Repository.org_id == Organization.id)
        .filter(
            Organization.workspace_id == workspace_id,
            Execution.status == ExecutionStatus.COMPLETED,
        )
        .group_by(Repository.name, Organization.provider)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )

    return [
        {"name": name, "provider": provider, "execution_count": count}
        for name, provider, count in rows
    ]
