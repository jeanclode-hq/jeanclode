"""``sentry_fix`` PR cleanup on a mid-run rate-limit hit (ADR-010).

A full-workflow restart re-triages before retrying, and ``TriageAgent``
searches ``gh pr list --state all --search ...`` — which matches on title
*or body*, and includes closed PRs. The leftover PR's body contains
the literal Sentry issue URLs and root-cause text
(``cli/src/workflows/sentry_fix/utils.py:pr_body``), so without this
cleanup a rate-limit retry would silently drop the Sentry issue instead of
retrying it: ``filter_and_route`` sees the matching PR and treats the issue
as already handled.

Deliberately done here — by the watcher, using the git provider credentials
it already holds (ADR-003) — rather than in the CLI's own exception
handler: a rate-limit failure can also arrive as a catastrophic container
death (OOM, hard timeout, kill) that never reaches an in-process ``except``
block at all. This runs independent of how the container exited, as long
as the failure got logged before it died.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import selectinload

from api.context import get_current_app
from api.database.organization import db_get_org_by_id
from api.models.executions import Execution
from api.models.issues import Issue

logger = logging.getLogger(__name__)

_GENERIC_TITLE = "Automated fix (superseded)"
_GENERIC_BODY = "This PR was superseded by a retry after an LLM rate limit. No action needed."

_TRAILING_NUMBER = re.compile(r"/(\d+)/?$")


def _parse_pr_number(pr_url: str) -> int | None:
    match = _TRAILING_NUMBER.search(pr_url)
    return int(match.group(1)) if match else None


async def cleanup_sentry_fix_pr(execution_id: UUID, rate_limit_info: dict[str, Any]) -> None:
    """Close the leftover PR/MR, delete its branch, and scrub its body.

    Best-effort: every failure path here just logs and returns — a missed
    cleanup means the next retry might mis-skip this issue (the pre-ADR-010
    behavior), not a crash. Nothing here is allowed to raise back into the
    caller's exit-handling path.
    """
    pr_url = rate_limit_info.get("pr_url")
    branch = rate_limit_info.get("branch")
    if not pr_url or not branch:
        logger.warning(
            "sentry_fix rate-limit cleanup skipped for execution %s — "
            "CLI event carried no branch/pr_url (group hadn't opened a PR yet)",
            execution_id,
        )
        return

    pr_number = _parse_pr_number(pr_url)
    if pr_number is None:
        logger.warning(
            "sentry_fix rate-limit cleanup skipped for execution %s — "
            "could not parse a PR number from %r",
            execution_id,
            pr_url,
        )
        return

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return

    with db_plugin.session() as db:
        execution = (
            db.query(Execution)
            .options(selectinload(Execution.issues).joinedload(Issue.repository))
            .filter(Execution.id == execution_id)
            .first()
        )
        if not execution or not execution.issues:
            logger.warning(
                "sentry_fix rate-limit cleanup skipped for execution %s — no linked issue",
                execution_id,
            )
            return

        # A batch is always issues for the same Sentry project (see
        # sentry/launch.py), so they share one mapped repo regardless of
        # which issue's group actually hit the rate limit.
        repository = execution.issues[0].repository
        mapped_repo = repository.mapped_repo if repository else None
        if not mapped_repo:
            logger.warning(
                "sentry_fix rate-limit cleanup skipped for execution %s — no mapped repo",
                execution_id,
            )
            return

        git_org = db_get_org_by_id(db, mapped_repo.org_id)
        if not git_org:
            return

        provider = git_org.provider
        installation_id = git_org.installation_id
        encrypted_token = mapped_repo.auth_token_encrypted or git_org.auth_token_encrypted
        base_url = git_org.base_url
        repo_name = mapped_repo.name
        external_id = mapped_repo.external_id

    if provider == "github":
        if not installation_id or not app.github:
            logger.warning(
                "sentry_fix rate-limit cleanup skipped for execution %s — "
                "no github installation available",
                execution_id,
            )
            return
        owner, _, repo_short = (repo_name or "").partition("/")
        try:
            token = await app.github.get_installation_access_token(installation_id)
            await app.github.close_and_scrub_pull_request(
                token, owner, repo_short, pr_number, title=_GENERIC_TITLE, body=_GENERIC_BODY
            )
            await app.github.delete_branch(token, owner, repo_short, branch)
        except Exception:
            logger.exception(
                "sentry_fix rate-limit cleanup failed for execution %s (github)", execution_id
            )
        return

    if provider == "gitlab":
        if not encrypted_token or not app.gitlab or not external_id:
            logger.warning(
                "sentry_fix rate-limit cleanup skipped for execution %s — "
                "no gitlab token/project id available",
                execution_id,
            )
            return
        try:
            token = db_plugin.decrypt(encrypted_token)
            await app.gitlab.close_and_scrub_merge_request(
                token,
                external_id,
                pr_number,
                title=_GENERIC_TITLE,
                description=_GENERIC_BODY,
                provider_url=base_url,
            )
            await app.gitlab.delete_branch(token, external_id, branch, provider_url=base_url)
        except Exception:
            logger.exception(
                "sentry_fix rate-limit cleanup failed for execution %s (gitlab)", execution_id
            )
        return

    logger.warning(
        "sentry_fix rate-limit cleanup skipped for execution %s — unsupported provider=%s",
        execution_id,
        provider,
    )
