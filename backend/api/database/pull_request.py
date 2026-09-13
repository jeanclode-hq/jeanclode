"""Database operations for PullRequest model."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, joinedload

from api.models.execution_links import execution_pull_requests, issue_pull_requests
from api.models.executions import Execution, ExecutionStatus, ExecutionWorkflow
from api.models.pull_requests import PRState, PullRequest
from api.models.repositories import Repository


def db_get_pull_request(
    db: Session,
    repository_id: UUID,
    pr_number: int,
) -> PullRequest | None:
    """Get a pull request by repository and PR number."""
    return (
        db.query(PullRequest)
        .filter(
            PullRequest.repository_id == repository_id,
            PullRequest.pr_number == pr_number,
        )
        .first()
    )


def db_get_pull_request_by_id(
    db: Session,
    pr_id: UUID,
) -> PullRequest | None:
    """Get a PR by id, eager-loading the repository (for org resolution)."""
    return (
        db.query(PullRequest)
        .options(joinedload(PullRequest.repository).joinedload(Repository.organization))
        .filter(PullRequest.id == pr_id)
        .first()
    )


def db_has_active_pr_execution(
    db: Session,
    pr_id: UUID,
    *,
    workflow: str | None = None,
) -> bool:
    """Check if a PR has a queued or running execution.

    Optionally scoped to a single workflow so a REVIEW execution doesn't
    block a SUMMARY trigger and vice versa.
    """
    query = (
        db.query(Execution.id)
        .join(execution_pull_requests, execution_pull_requests.c.execution_id == Execution.id)
        .filter(
            execution_pull_requests.c.pull_request_id == pr_id,
            Execution.status.in_([ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]),
        )
    )
    if workflow is not None:
        query = query.filter(Execution.workflow == workflow)
    return query.first() is not None


def db_upsert_pull_request(
    db: Session,
    repository_id: UUID,
    pr_number: int,
    title: str,
    author: str,
    state: PRState,
    pr_url: str,
    head_branch: str,
    base_branch: str,
    external_pr_id: str | None = None,
    head_sha: str | None = None,
    created_at: datetime | None = None,
) -> tuple[PullRequest, bool]:
    """Create or update a pull request.

    Returns:
        Tuple of (PullRequest, created) where created is True if new.
    """
    pr = db_get_pull_request(db, repository_id, pr_number)

    if pr:
        # Update mutable fields — author is immutable (set only on creation,
        # since event.user on close/merge is the actor, not the original author)
        pr.title = title
        pr.state = state.value
        pr.pr_url = pr_url
        pr.head_branch = head_branch
        pr.base_branch = base_branch
        pr.head_sha = head_sha
        if external_pr_id:
            pr.external_pr_id = external_pr_id
        db.commit()
        db.refresh(pr)
        return pr, False

    pr = PullRequest(
        repository_id=repository_id,
        pr_number=pr_number,
        external_pr_id=external_pr_id,
        title=title,
        author=author,
        state=state.value,
        pr_url=pr_url,
        head_branch=head_branch,
        base_branch=base_branch,
        head_sha=head_sha,
        **({"created_at": created_at} if created_at else {}),
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr, True


def db_link_issue_pull_requests(
    db: Session,
    pr_id: UUID,
    issue_ids: Sequence[UUID],
) -> None:
    """Link a pull request to the issue(s) it specifically addresses.

    Idempotent — rows already present are skipped. A FIX execution batches
    several issues and synthesis can split that batch into multiple PRs, one
    per root-cause group, so ``execution_pull_requests`` alone can't say which
    PR out of the batch addresses a given issue. This link records that.
    """
    if not issue_ids:
        return
    existing = set(
        db.execute(
            select(issue_pull_requests.c.issue_id).where(
                issue_pull_requests.c.pull_request_id == pr_id
            )
        ).scalars()
    )
    new_ids = [iid for iid in dict.fromkeys(issue_ids) if iid not in existing]
    if not new_ids:
        return
    db.execute(
        issue_pull_requests.insert(),
        [{"issue_id": iid, "pull_request_id": pr_id} for iid in new_ids],
    )
    db.commit()


def db_git_org_has_open_fix_pr(db: Session, git_org_id: UUID) -> bool:
    """True while any repo under ``git_org_id`` has an open FIX-linked PR/MR.

    The merge gate (Part E2), re-checked inside ``_claim_batch`` after the
    eligibility query in case a PR opened in between. Only ``state = 'open'``
    counts — closed or merged is done. Bounded by the small number of
    outstanding bot PRs per git org.
    """
    return db.query(
        exists(
            select(PullRequest.id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .join(
                execution_pull_requests,
                execution_pull_requests.c.pull_request_id == PullRequest.id,
            )
            .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
            .where(
                Repository.org_id == git_org_id,
                Execution.workflow == ExecutionWorkflow.FIX.value,
                PullRequest.state == PRState.OPEN.value,
            )
        )
    ).scalar()


def db_get_open_fix_prs_for_git_org(
    db: Session, git_org_id: UUID
) -> list[tuple[PullRequest, Repository]]:
    """Open FIX-linked ``(PullRequest, Repository)`` rows under a git org.

    The input to active reconciliation (Part E1): the DB believes these PRs
    are open, so the provider is asked whether that is still true. Closed and
    merged rows are terminal and never re-polled, so they're excluded here.
    """
    return (
        db.query(PullRequest, Repository)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(
            execution_pull_requests,
            execution_pull_requests.c.pull_request_id == PullRequest.id,
        )
        .join(Execution, Execution.id == execution_pull_requests.c.execution_id)
        .filter(
            Repository.org_id == git_org_id,
            Execution.workflow == ExecutionWorkflow.FIX.value,
            PullRequest.state == PRState.OPEN.value,
        )
        .distinct()
        .all()
    )


def resolve_workspace_id(pr: PullRequest) -> str | None:
    """Traverse PullRequest → Repository → Organization → workspace_id."""
    try:
        repo = pr.repository
        if repo and repo.organization:
            wid = repo.organization.workspace_id
            return str(wid) if wid else None
    except Exception:
        return None
    return None
