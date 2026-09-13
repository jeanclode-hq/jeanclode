"""GitLab @jeanclode mention handler — ``note`` events.

Mirrors :mod:`api.routers.webhooks.github.mentions` for GitLab. GitLab's
``note`` event covers MR comments, MR inline diff replies, and Issue
comments, discriminated by ``object_attributes.noteable_type`` and the
presence of ``object_attributes.position`` (set → inline thread).

The dispatch event carries only the note URL — the CLI re-fetches the
note from the GitLab API for body / surface metadata.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.database import run_in_session
from api.database.execution import db_create_execution, db_has_active_execution
from api.database.issue import db_create_issue, db_get_issue_by_sentry_id
from api.database.organization import db_get_org_by_id, db_resolve_org_settings
from api.database.pull_request import (
    db_get_pull_request,
    db_has_active_pr_execution,
    db_upsert_pull_request,
)
from api.database.repository import db_get_repository_by_external_id
from api.models.executions import ExecutionStatus, ExecutionTrigger, ExecutionWorkflow
from api.models.pull_requests import PRState
from api.models.respond_dispatch import RespondDispatchPayload
from api.models.settings import GitOrgSettings, TriggerPermission
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.plugins.gitlab.permissions import fetch_member_access_level, is_authorized
from api.routers.webhooks.git_dispatch import repo_is_enabled

from ..utils import WebhookResponse


class _Surface(StrEnum):
    """Mention surface — logged only, never transmitted."""

    PR_TOP_LEVEL = "pr_top_level"
    PR_INLINE_THREAD = "pr_inline_thread"
    PR_REVIEW_SUBMISSION = "pr_review_submission"
    ISSUE = "issue"


logger = logging.getLogger(__name__)


_MENTION_RE = re.compile(r"(?<![\w.-])@jeanclode-bot(?![\w-])", re.IGNORECASE)


class _NormalizedNote(BaseModel):
    """Provider-agnostic shape derived from a GitLab note event."""

    surface: _Surface
    body: str
    sender_login: str
    sender_id: int | None = None
    sender_is_bot: bool = False
    repo_external_id: str
    repo_full_name: str = ""
    pr_number: int | None = None
    issue_number: int | None = None
    pr_title: str = ""
    pr_author: str = ""
    pr_state: str = "opened"
    pr_url: str = ""
    head_branch: str = ""
    base_branch: str = ""
    head_sha: str = ""
    external_pr_id: str = ""
    target_url: str = ""
    comment_id: str = ""
    thread_id: str = ""
    issue_title: str = ""
    issue_url: str = ""
    issue_author: str = ""

    model_config = {"extra": "ignore"}


def has_mention(body: str) -> bool:
    """True iff ``body`` contains a stand-alone ``@jeanclode`` token."""
    return bool(_MENTION_RE.search(body or ""))


def _is_bot_login(login: str) -> bool:
    """True for GitLab bot accounts.

    GitLab project/group tokens have ``_bot`` (or ``_bot_<suffix>``) in
    their username. We accept the exact ``jeanclode-bot`` username for
    parity with the GitHub side; everything else needs the ``_bot``
    marker so substrings like ``notjeanclode`` don't get misclassified.
    """
    if not login:
        return False
    lower = login.lower()
    if lower == "jeanclode-bot":
        return True
    return lower.endswith("_bot") or "_bot_" in lower or lower.endswith("[bot]")


def _note_target_url(note: dict, fallback_url: str) -> str:
    """Return a note URL that always carries the ``#note_<id>`` fragment.

    Some self-hosted GitLab versions omit the fragment from
    ``object_attributes.url``.  We reconstruct it from ``note["id"]`` so
    that the CLI's ``parse_note_url()`` can always extract the identifier
    and populate ``.context/``.
    """
    note_id = note.get("id")
    raw = note.get("url") or fallback_url
    if note_id and raw and f"#note_{note_id}" not in raw:
        return f"{raw.split('#')[0]}#note_{note_id}"
    return raw


def _normalize(payload: dict) -> _NormalizedNote | None:
    """Convert a raw GitLab note webhook into a :class:`_NormalizedNote`.

    Returns ``None`` when the event isn't a creation (GitLab may send
    notes on resolution events too) or required fields are missing.
    """
    note = payload.get("object_attributes") or {}
    if note.get("action") and note.get("action") != "create":
        # GitLab also emits notes on resolve/unresolve — skip those.
        return None

    project = payload.get("project") or {}
    user = payload.get("user") or {}
    repo_external_id = str(project.get("id") or "")
    if not repo_external_id:
        return None
    repo_full_name = project.get("path_with_namespace") or ""
    sender_login = user.get("username") or ""
    sender_id = user.get("id")

    body = note.get("note") or ""
    noteable_type = (note.get("noteable_type") or "").lower()

    sender_is_bot = _is_bot_login(sender_login)

    if noteable_type == "mergerequest" or noteable_type == "merge_request":
        mr = payload.get("merge_request") or {}
        position = note.get("position") or note.get("original_position") or None
        surface = _Surface.PR_INLINE_THREAD if position else _Surface.PR_TOP_LEVEL
        return _NormalizedNote(
            surface=surface,
            body=body,
            sender_login=sender_login,
            sender_id=sender_id,
            sender_is_bot=sender_is_bot,
            repo_external_id=repo_external_id,
            repo_full_name=repo_full_name,
            pr_number=int(mr.get("iid") or 0),
            pr_title=mr.get("title") or "",
            pr_author=(mr.get("author") or {}).get("username") or str(mr.get("author_id") or ""),
            pr_state=mr.get("state") or "opened",
            pr_url=mr.get("url") or "",
            head_branch=mr.get("source_branch") or "",
            base_branch=mr.get("target_branch") or "",
            head_sha=(mr.get("last_commit") or {}).get("id") or "",
            external_pr_id=str(mr.get("id") or ""),
            target_url=_note_target_url(note, mr.get("url") or ""),
            comment_id=str(note.get("id") or ""),
            thread_id=note.get("discussion_id") or "",
        )

    if noteable_type == "issue":
        issue = payload.get("issue") or {}
        return _NormalizedNote(
            surface=_Surface.ISSUE,
            body=body,
            sender_login=sender_login,
            sender_id=sender_id,
            sender_is_bot=sender_is_bot,
            repo_external_id=repo_external_id,
            repo_full_name=repo_full_name,
            issue_number=int(issue.get("iid") or 0),
            issue_title=issue.get("title") or "",
            issue_url=issue.get("url") or "",
            issue_author=(issue.get("author") or {}).get("username")
            or str(issue.get("author_id") or ""),
            target_url=_note_target_url(note, issue.get("url") or ""),
            comment_id=str(note.get("id") or ""),
            thread_id=note.get("discussion_id") or "",
        )

    return None


class _Gate(NamedTuple):
    """The repo/org facts the network and reserve phases need."""

    repo_id: UUID
    org_id: UUID
    trigger_permission: TriggerPermission


async def handle_note_event(payload: dict) -> WebhookResponse:
    """Process a GitLab note event end-to-end."""
    normalized = _normalize(payload)
    if not normalized:
        return WebhookResponse(message="note not actionable", processed=False)
    if not has_mention(normalized.body):
        return WebhookResponse(message="note: no @jeanclode mention", processed=False)

    def _gate(db: Session) -> WebhookResponse | _Gate:
        repository = db_get_repository_by_external_id(db, normalized.repo_external_id)
        if not repository:
            return WebhookResponse(
                message=f"Repository {normalized.repo_full_name or normalized.repo_external_id} not tracked",
                processed=False,
            )

        if not repo_is_enabled(repository):
            return WebhookResponse(message="repository is disabled", processed=False)

        org = db_get_org_by_id(db, repository.org_id)
        if not org:
            return WebhookResponse(message="Org missing for repo", processed=False)

        settings = GitOrgSettings.model_validate(db_resolve_org_settings(db, org))
        return _Gate(
            repo_id=repository.id, org_id=org.id, trigger_permission=settings.trigger_permission
        )

    gate = await run_in_session(_gate)
    if isinstance(gate, WebhookResponse):
        return gate

    if not normalized.sender_is_bot and gate.trigger_permission != TriggerPermission.ANYONE:
        if normalized.sender_id is None:
            logger.warning(
                "Note from %s on %s missing sender_id — cannot verify authorization",
                normalized.sender_login,
                normalized.repo_full_name,
            )
            return WebhookResponse(
                message="sender_id missing — cannot verify authorization", processed=False
            )
        access_level = await fetch_member_access_level(
            org_id=gate.org_id,
            project_id=normalized.repo_external_id,
            user_id=normalized.sender_id,
        )
        if not is_authorized(access_level):
            logger.info(
                "Dropping note from unauthorized user %s on %s (level=%s)",
                normalized.sender_login,
                normalized.repo_full_name,
                access_level,
            )
            return WebhookResponse(message="commenter not authorized", processed=False)

    def _reserve(db: Session) -> WebhookResponse | RespondDispatchPayload:
        repository_id = gate.repo_id
        pr_id: str | None = None
        issue_id: str | None = None
        queued_behind_active = False
        if normalized.pr_number:
            pr = db_get_pull_request(db, repository_id, normalized.pr_number)
            if not pr:
                if not normalized.pr_url:
                    return WebhookResponse(message="MR not found and no url", processed=False)
                try:
                    state = PRState(_GITLAB_STATE_MAP.get(normalized.pr_state, PRState.OPEN.value))
                except ValueError:
                    state = PRState.OPEN
                pr, _ = db_upsert_pull_request(
                    db,
                    repository_id=repository_id,
                    pr_number=normalized.pr_number,
                    title=normalized.pr_title or f"MR !{normalized.pr_number}",
                    author=normalized.pr_author or "unknown",
                    state=state,
                    pr_url=normalized.pr_url,
                    head_branch=normalized.head_branch,
                    base_branch=normalized.base_branch,
                    external_pr_id=normalized.external_pr_id or None,
                    head_sha=normalized.head_sha or None,
                )
            normalized.pr_author = normalized.pr_author or pr.author
            queued_behind_active = db_has_active_pr_execution(
                db, pr.id, workflow=ExecutionWorkflow.RESPOND.value
            )
            if queued_behind_active:
                logger.info(
                    "Queuing respond dispatch for MR %s behind an in-flight respond execution",
                    pr.id,
                )
            execution = db_create_execution(
                db,
                provider="gitlab",
                pull_requests=[pr],
                workflow=ExecutionWorkflow.RESPOND.value,
                trigger=ExecutionTrigger.AUTO.value,
                status=ExecutionStatus.QUEUED.value,
                retry_target_url=normalized.target_url,
                prompt_text=normalized.body,
            )
            pr_id = str(pr.id)
        elif normalized.issue_number:
            external_id = str(normalized.issue_number)
            issue = db_get_issue_by_sentry_id(db, repository_id, external_id)
            if not issue:
                issue = db_create_issue(
                    db=db,
                    repository_id=repository_id,
                    external_id=external_id,
                    title=normalized.issue_title or f"Issue #{external_id}",
                    level="info",
                    status="open",
                    author=normalized.issue_author or "unknown",
                    issue_url=normalized.issue_url or "",
                )
            queued_behind_active = db_has_active_execution(
                db, issue.id, workflow=ExecutionWorkflow.RESPOND.value
            )
            if queued_behind_active:
                logger.info(
                    "Queuing respond dispatch for issue %s behind an in-flight respond execution",
                    issue.id,
                )
            execution = db_create_execution(
                db,
                provider="gitlab",
                issues=[issue],
                workflow=ExecutionWorkflow.RESPOND.value,
                trigger=ExecutionTrigger.AUTO.value,
                status=ExecutionStatus.QUEUED.value,
                retry_target_url=normalized.target_url,
                prompt_text=normalized.body,
            )
            issue_id = str(issue.id)
        else:
            return WebhookResponse(message="no MR or issue iid on event", processed=False)

        return RespondDispatchPayload(
            execution_id=str(execution.id),
            organization_id=str(gate.org_id),
            target_url=normalized.target_url,
            pull_request_id=pr_id,
            issue_id=issue_id,
            queued_behind_active=queued_behind_active,
        )

    reserved = await run_in_session(_reserve)
    if isinstance(reserved, WebhookResponse):
        return reserved

    if reserved.queued_behind_active:
        logger.info(
            "Respond dispatch queued behind an active execution (gitlab): execution=%s "
            "surface=%s mr=%s issue=%s",
            reserved.execution_id,
            normalized.surface,
            reserved.pull_request_id,
            reserved.issue_id,
        )
        return WebhookResponse(
            message=f"respond execution {reserved.execution_id} queued behind an in-flight respond",
            processed=True,
        )

    broker = get_faststream_broker()
    await broker.publish(
        reserved.model_dump(mode="json"),
        stream="jeanclode.events.gitlab.manual_dispatch",
        maxlen=STREAM_MAXLEN,
    )
    logger.info(
        "Respond dispatch queued (gitlab): execution=%s surface=%s mr=%s issue=%s",
        reserved.execution_id,
        normalized.surface,
        reserved.pull_request_id,
        reserved.issue_id,
    )
    return WebhookResponse(
        message=f"respond execution {reserved.execution_id} queued for {normalized.surface}",
        processed=True,
    )


_GITLAB_STATE_MAP = {
    "opened": PRState.OPEN.value,
    "reopened": PRState.OPEN.value,
    "closed": PRState.CLOSED.value,
    "merged": PRState.MERGED.value,
}


__all__ = ["handle_note_event", "has_mention"]
