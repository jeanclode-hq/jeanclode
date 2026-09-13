"""GitHub @jeanclode mention handler.

Consumes ``issue_comment`` / ``pull_request_review`` /
``pull_request_review_comment`` events that already passed signature
verification, applies gating (org trigger setting + author
authorization), and publishes a respond dispatch event whose only payload
is the comment URL — the CLI re-fetches the comment body and surface
metadata from the GitHub API.

Surface tagging is internal to this module — we log it but never
transmit it. The CLI derives the surface from the URL fragment.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.context import get_current_app
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
from api.plugins.github.permissions import fetch_collaborator_permission, is_authorized
from api.routers.webhooks.git_dispatch import repo_is_enabled

from ..utils import WebhookResponse

logger = logging.getLogger(__name__)


class _Surface(StrEnum):
    """Mention surface — logged only.

    Not transmitted to the container; the CLI derives surface from the
    URL fragment (``#discussion_r…`` / ``#issuecomment-…`` / etc.).
    """

    PR_TOP_LEVEL = "pr_top_level"
    PR_INLINE_THREAD = "pr_inline_thread"
    PR_REVIEW_SUBMISSION = "pr_review_submission"
    ISSUE = "issue"


# The trigger token aligns with the GitHub App's slug (``jeanclode-bot``)
# so users can also literally @-mention the bot account. The negative
# lookahead ``(?![\w-])`` keeps suffixed handles like ``@jeanclode-bot-fan``
# from triggering — plain ``\b`` wouldn't, since ``t`` to ``-`` counts as
# a word boundary.
_MENTION_RE = re.compile(r"(?<![\w.-])@jeanclode-bot(?![\w-])", re.IGNORECASE)


class _NormalizedEvent(BaseModel):
    """Provider-agnostic shape derived from a GitHub mention event."""

    surface: _Surface
    body: str
    sender_login: str
    sender_is_bot: bool
    repo_external_id: str
    repo_full_name: str = ""
    pr_number: int | None = None
    issue_number: int | None = None
    pr_title: str = ""
    pr_author: str = ""
    pr_state: str = "open"
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


def _is_bot_login(login: str) -> bool:
    return login.endswith("[bot]")


def has_mention(body: str) -> bool:
    """True iff ``body`` contains a stand-alone ``@jeanclode`` token."""
    return bool(_MENTION_RE.search(body or ""))


def _normalize(event_type: str, payload: dict) -> _NormalizedEvent | None:
    """Convert a raw GitHub webhook into a :class:`_NormalizedEvent`.

    Returns ``None`` for events we don't act on (e.g., a deleted comment,
    or an event missing the fields we need).
    """
    action = payload.get("action") or ""
    if action in {"deleted", "edited"}:
        # Only fire on creation. Editing a comment to add @jeanclode after
        # the fact would require re-running, which would surprise users.
        return None

    repo = payload.get("repository") or {}
    repo_external_id = str(repo.get("id") or "")
    if not repo_external_id:
        return None
    repo_full_name = repo.get("full_name") or ""
    sender = payload.get("sender") or {}
    sender_login = sender.get("login") or ""
    sender_is_bot = (sender.get("type") or "").lower() == "bot" or _is_bot_login(sender_login)

    if event_type == "issue_comment":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        body = comment.get("body") or ""
        is_pr = bool(issue.get("pull_request"))
        if is_pr:
            # ``issue_comment`` payloads do NOT carry a top-level
            # ``pull_request`` object — only the issue resource (which
            # for a PR carries the PR's ``html_url`` and metadata).
            return _NormalizedEvent(
                surface=_Surface.PR_TOP_LEVEL,
                body=body,
                sender_login=sender_login,
                sender_is_bot=sender_is_bot,
                repo_external_id=repo_external_id,
                repo_full_name=repo_full_name,
                pr_number=int(issue.get("number") or 0),
                pr_title=issue.get("title") or "",
                pr_author=(issue.get("user") or {}).get("login") or "",
                pr_state=issue.get("state") or "open",
                pr_url=issue.get("html_url") or "",
                target_url=comment.get("html_url") or issue.get("html_url") or "",
                comment_id=str(comment.get("id") or ""),
            )
        return _NormalizedEvent(
            surface=_Surface.ISSUE,
            body=body,
            sender_login=sender_login,
            sender_is_bot=sender_is_bot,
            repo_external_id=repo_external_id,
            repo_full_name=repo_full_name,
            issue_number=int(issue.get("number") or 0),
            issue_title=issue.get("title") or "",
            issue_url=issue.get("html_url") or "",
            issue_author=(issue.get("user") or {}).get("login") or "",
            target_url=comment.get("html_url") or issue.get("html_url") or "",
            comment_id=str(comment.get("id") or ""),
        )

    if event_type == "pull_request_review":
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        body = review.get("body") or ""
        if not body:
            return None
        return _NormalizedEvent(
            surface=_Surface.PR_REVIEW_SUBMISSION,
            body=body,
            sender_login=sender_login,
            sender_is_bot=sender_is_bot,
            repo_external_id=repo_external_id,
            repo_full_name=repo_full_name,
            pr_number=int(pr.get("number") or 0),
            target_url=review.get("html_url") or pr.get("html_url") or "",
            comment_id=str(review.get("id") or ""),
        )

    if event_type == "pull_request_review_comment":
        comment = payload.get("comment") or {}
        pr = payload.get("pull_request") or {}
        body = comment.get("body") or ""
        return _NormalizedEvent(
            surface=_Surface.PR_INLINE_THREAD,
            body=body,
            sender_login=sender_login,
            sender_is_bot=sender_is_bot,
            repo_external_id=repo_external_id,
            repo_full_name=repo_full_name,
            pr_number=int(pr.get("number") or 0),
            target_url=comment.get("html_url") or pr.get("html_url") or "",
            comment_id=str(comment.get("id") or ""),
            thread_id=str(
                comment.get("pull_request_review_id") or comment.get("in_reply_to_id") or ""
            ),
        )

    return None


def _enrich_pr_fields(normalized: _NormalizedEvent, payload: dict) -> None:
    """Populate PR-specific fields on the normalized event from the raw payload.

    GitHub sends the full PR object on review/review_comment events; on
    issue_comment, we have to fetch via the DB later. Splitting this out
    lets the normalize() path stay declarative.
    """
    pr = payload.get("pull_request") or {}
    if not pr:
        return
    normalized.pr_title = pr.get("title") or ""
    normalized.pr_author = (pr.get("user") or {}).get("login") or ""
    normalized.pr_state = pr.get("state") or "open"
    normalized.pr_url = pr.get("html_url") or normalized.pr_url
    normalized.head_branch = (pr.get("head") or {}).get("ref") or ""
    normalized.base_branch = (pr.get("base") or {}).get("ref") or ""
    normalized.head_sha = (pr.get("head") or {}).get("sha") or ""
    normalized.external_pr_id = str(pr.get("id") or "")


class _Gate(NamedTuple):
    """The repo/org facts the network and reserve phases need."""

    repo_id: UUID
    org_id: UUID
    installation_id: str | None
    trigger_permission: TriggerPermission


async def handle_mention_event(event_type: str, payload: dict) -> WebhookResponse:
    """Process a normalized mention event end-to-end.

    Steps: normalize → mention regex → repo lookup → org settings gate →
    author auth → upsert PR/issue → create execution → publish dispatch
    event.
    """
    normalized = _normalize(event_type, payload)
    if not normalized:
        return WebhookResponse(message=f"{event_type} not actionable", processed=False)
    if not has_mention(normalized.body):
        return WebhookResponse(message=f"{event_type}: no @jeanclode mention", processed=False)

    _enrich_pr_fields(normalized, payload)

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
            repo_id=repository.id,
            org_id=org.id,
            installation_id=org.installation_id,
            trigger_permission=settings.trigger_permission,
        )

    gate = await run_in_session(_gate)
    if isinstance(gate, WebhookResponse):
        return gate

    # Author authorization. We need an installation token to call
    # ``/repos/.../collaborators/{user}/permission``. Bots skip the perm
    # check since the collab API doesn't return a meaningful permission
    # for bot logins. The org can also open this up to anyone via
    # ``trigger_permission`` — same bypass, different reason.
    installation_id = gate.installation_id
    if not normalized.sender_is_bot and gate.trigger_permission != TriggerPermission.ANYONE:
        if not installation_id:
            logger.warning("Cannot check perms: github org %s has no installation_id", gate.org_id)
            return WebhookResponse(message="installation_id missing", processed=False)

        github_plugin = get_current_app().github
        if not github_plugin:
            return WebhookResponse(message="github plugin unavailable", processed=False)
        try:
            install_token = await github_plugin.get_installation_access_token(installation_id)
        except Exception:
            logger.exception("Failed to mint installation token for org %s", gate.org_id)
            return WebhookResponse(message="installation token failed", processed=False)

        owner, _, repo_name = (normalized.repo_full_name or "").partition("/")
        if not owner or not repo_name:
            logger.error(
                "Cannot resolve owner/repo from full_name=%r — refusing to dispatch",
                normalized.repo_full_name,
            )
            return WebhookResponse(message="invalid repo identifier", processed=False)
        permission = await fetch_collaborator_permission(
            installation_token=install_token,
            owner=owner,
            repo=repo_name,
            username=normalized.sender_login,
        )
        if not is_authorized(permission):
            logger.info(
                "Dropping mention from unauthorized user %s on %s (perm=%s)",
                normalized.sender_login,
                normalized.repo_full_name,
                permission,
            )
            return WebhookResponse(message="commenter not authorized", processed=False)

    # Resolve target row (PR or issue) and create the execution.
    def _reserve(db: Session) -> WebhookResponse | RespondDispatchPayload:
        pr_id: str | None = None
        issue_id: str | None = None
        queued_behind_active = False
        repository_id = gate.repo_id
        if normalized.pr_number:
            pr = db_get_pull_request(db, repository_id, normalized.pr_number)
            if not pr:
                # Webhook arrived before the upsert path ran (rare, but
                # possible for inline review comments on a fresh PR). Best-
                # effort upsert from the partial fields we have.
                if not normalized.pr_url:
                    return WebhookResponse(message="PR not found and no url", processed=False)
                try:
                    state = PRState(normalized.pr_state)
                except ValueError:
                    state = PRState.OPEN
                pr, _ = db_upsert_pull_request(
                    db,
                    repository_id=repository_id,
                    pr_number=normalized.pr_number,
                    title=normalized.pr_title or f"PR #{normalized.pr_number}",
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
                    "Queuing respond dispatch for PR %s behind an in-flight respond execution",
                    pr.id,
                )
            execution = db_create_execution(
                db,
                provider="github",
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
                provider="github",
                issues=[issue],
                workflow=ExecutionWorkflow.RESPOND.value,
                trigger=ExecutionTrigger.AUTO.value,
                status=ExecutionStatus.QUEUED.value,
                retry_target_url=normalized.target_url,
                prompt_text=normalized.body,
            )
            issue_id = str(issue.id)
        else:
            return WebhookResponse(message="no PR or issue number on event", processed=False)

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
            "Respond dispatch queued behind an active execution: execution=%s surface=%s "
            "pr=%s issue=%s author=%s",
            reserved.execution_id,
            normalized.surface,
            reserved.pull_request_id,
            reserved.issue_id,
            normalized.sender_login,
        )
        return WebhookResponse(
            message=f"respond execution {reserved.execution_id} queued behind an in-flight respond",
            processed=True,
        )

    broker = get_faststream_broker()
    await broker.publish(
        reserved.model_dump(mode="json"),
        stream="jeanclode.events.github.manual_dispatch",
        maxlen=STREAM_MAXLEN,
    )

    logger.info(
        "Respond dispatch queued: execution=%s surface=%s pr=%s issue=%s author=%s",
        reserved.execution_id,
        normalized.surface,
        reserved.pull_request_id,
        reserved.issue_id,
        normalized.sender_login,
    )
    return WebhookResponse(
        message=f"respond execution {reserved.execution_id} queued for {normalized.surface}",
        processed=True,
    )


__all__ = ["handle_mention_event", "has_mention"]
