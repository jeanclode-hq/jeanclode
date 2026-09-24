"""Persist what an ``issue_resolve`` run reports: the triage verdict and the PRs it opened.

Links go through ``execution_pull_requests`` / ``issue_pull_requests`` like
Sentry fixes do, but under the ISSUE_RESOLVE workflow — the Sentry merge gate
and fix-PR reconciler filter on FIX, so these PRs never hold a Sentry batch.
"""

import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session, selectinload

from api.database.execution import db_link_execution_pull_requests
from api.database.issue import db_update_issue
from api.database.pull_request import (
    db_get_pull_request,
    db_link_issue_pull_requests,
    db_upsert_pull_request,
)
from api.models.executions import Execution, ExecutionWorkflow
from api.models.issues import Issue, TriageResult
from api.models.pull_requests import PRState
from api.models.repositories import Repository
from api.plugins.container.related import resolve_related_repos

logger = logging.getLogger(__name__)

_GIT_PROVIDERS = frozenset({"github", "gitlab"})
_PR_NUMBER_RE = re.compile(r"/(?:pull|merge_requests)/(\d+)/?$")


def normalize_web_url(url: str | None) -> str:
    """Lowercase, scheme-stripped, ``.git``/slash-trimmed URL for prefix matching."""
    if not url:
        return ""
    u = url.strip().lower()
    for prefix in ("https://", "http://", "git@"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
            break
    return u.rstrip("/").removesuffix(".git")


def match_repo_by_url(pr_url: str, candidates: list[Repository]) -> Repository | None:
    normalized = normalize_web_url(pr_url)
    for repo in candidates:
        prefix = normalize_web_url(repo.web_url)
        if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
            return repo
    return None


def persist_issue_resolve_result(
    db: Session, execution_id: uuid.UUID, result: dict[str, Any]
) -> None:
    """Record the triage verdict and opened PRs of a GitHub/GitLab ``issue_resolve`` run."""
    data = (result or {}).get("data") or {}
    triage_value = data.get("triage_result")
    pr_urls = [url for url in data.get("pr_urls") or [] if url]
    if not triage_value and not pr_urls:
        return

    execution = (
        db.query(Execution)
        .options(
            selectinload(Execution.issues)
            .joinedload(Issue.repository)
            .joinedload(Repository.organization)
        )
        .filter(Execution.id == execution_id)
        .first()
    )
    if execution is None or execution.workflow != ExecutionWorkflow.ISSUE_RESOLVE.value:
        return
    if len(execution.issues) != 1:
        return
    issue = execution.issues[0]
    repo = issue.repository
    if repo is None or repo.organization is None:
        return
    provider = repo.organization.provider
    if provider not in _GIT_PROVIDERS:
        return

    if triage_value in (TriageResult.ACTIONABLE.value, TriageResult.NOT_ACTIONABLE.value):
        db_update_issue(db, issue.id, triage_result=triage_value)

    if not pr_urls:
        return

    # A git token only reaches its own provider, so a PR URL elsewhere is not ours.
    candidates = [r for r in [repo, *resolve_related_repos(db, repo)] if r.provider == provider]
    pr_ids: list[uuid.UUID] = []
    for pr_url in pr_urls:
        number_match = _PR_NUMBER_RE.search(pr_url)
        target = match_repo_by_url(pr_url, candidates)
        if number_match is None or target is None:
            logger.warning(
                "persist issue_resolve PRs: could not match %s (execution %s)",
                pr_url,
                execution_id,
            )
            continue
        pr_number = int(number_match.group(1))

        existing = db_get_pull_request(db, target.id, pr_number)
        if existing is not None:
            pr_ids.append(existing.id)
            continue
        pr, _ = db_upsert_pull_request(
            db,
            repository_id=target.id,
            pr_number=pr_number,
            title=issue.title or "Automated fix",
            author="jeanclode-bot",
            state=PRState.OPEN,
            pr_url=pr_url,
            head_branch="",
            base_branch="",
        )
        pr_ids.append(pr.id)

    for pr_id in pr_ids:
        db_link_issue_pull_requests(db, pr_id, [issue.id])
    if pr_ids:
        db_link_execution_pull_requests(db, execution_id, pr_ids)
