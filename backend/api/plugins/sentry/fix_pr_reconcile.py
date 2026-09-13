"""Active reconciliation of open fix PRs before a Sentry batch dispatch (Part E1).

The merge gate (Part E2) blocks a ``(sentry_org, git_org)`` pair while any
FIX-linked PR under that git org is still ``open`` in our DB. Webhooks
normally keep that state live, but a dropped delivery would strand the gate
closed forever. This poll runs at the top of ``_dispatch_target`` (only when
the Sentry org enabled the gate) and asks the provider the one question the
gate cares about: *is a PR the DB believes is open actually still open?*

Terminal rows (``closed`` / ``merged``) are never re-polled. Every provider
call is best-effort — a failure falls back to the last-known DB state and the
tick proceeds; only a definitive 404 flips a row to ``closed``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from api.context import get_current_app
from api.database.organization import db_get_org_by_id
from api.database.pull_request import db_get_open_fix_prs_for_git_org
from api.models.pull_requests import PRState, PullRequest
from api.routers.webhooks.github.utils import resolve_pr_state
from api.sse.publishers import publish_pull_request_event

logger = logging.getLogger(__name__)

_GITLAB_MR_STATE: dict[str, PRState] = {
    "opened": PRState.OPEN,
    "locked": PRState.OPEN,
    "reopened": PRState.OPEN,
    "closed": PRState.CLOSED,
    "merged": PRState.MERGED,
}


def _resolve_gitlab_state(mr: dict[str, Any]) -> PRState:
    return _GITLAB_MR_STATE.get(mr.get("state", "opened"), PRState.OPEN)


async def reconcile_open_fix_prs(db_plugin: Any, git_org_id: UUID) -> None:
    """Refresh DB state for every open FIX-linked PR under ``git_org_id``."""
    app = get_current_app()

    def _load(db) -> tuple[Any, ...] | None:
        git_org = db_get_org_by_id(db, git_org_id)
        if not git_org:
            return None
        rows = db_get_open_fix_prs_for_git_org(db, git_org_id)
        prs = [
            {
                "pr_id": pr.id,
                "pr_number": pr.pr_number,
                "pr_url": pr.pr_url,
                "state": pr.state,
                "title": pr.title,
                "author": pr.author,
                "head_branch": pr.head_branch,
                "base_branch": pr.base_branch,
                "repo_name": repo.name,
                "repo_external_id": repo.external_id,
                "repo_token_encrypted": repo.auth_token_encrypted,
            }
            for pr, repo in rows
        ]
        workspace_id = str(git_org.workspace_id) if git_org.workspace_id else ""
        return (
            git_org.provider,
            workspace_id,
            git_org.installation_id,
            git_org.auth_token_encrypted,
            git_org.base_url,
            prs,
        )

    loaded = await db_plugin.run_in_session(_load)
    if not loaded:
        return
    provider, workspace_id, installation_id, org_token_encrypted, base_url, prs = loaded
    if not prs:
        return

    # Resolve the org-wide token once, not once per PR: a GitHub installation
    # token covers every repo under it, the GitLab group token every project.
    # Minting per PR would re-sign a JWT and round-trip GitHub for every
    # outstanding bot PR. GitLab's rare per-repo override is still honoured
    # in _fetch_state.
    org_token: str | None
    if provider == "github":
        if not installation_id or not app.github:
            return
        try:
            org_token = await app.github.get_installation_access_token(installation_id)
        except Exception:
            logger.warning("fix PR reconcile: github token mint failed", exc_info=True)
            return
    elif provider == "gitlab":
        if not app.gitlab:
            return
        org_token = _decrypt(db_plugin, org_token_encrypted)
    else:
        return

    for pr in prs:
        new_state = await _fetch_state(
            provider,
            pr,
            app=app,
            db_plugin=db_plugin,
            org_token=org_token,
            base_url=base_url,
        )
        if new_state is None or new_state.value == pr["state"]:
            continue

        await db_plugin.run_in_session(
            lambda db, pr=pr, new_state=new_state: _update_pr_state(db, pr, new_state)
        )
        await publish_pull_request_event(
            workspace_id=workspace_id,
            action="updated",
            payload={
                "pull_request_id": str(pr["pr_id"]),
                "pr_number": pr["pr_number"],
                "state": new_state.value,
                "pr_url": pr["pr_url"],
            },
        )
        logger.info(
            "Reconciled fix PR %s: %s -> %s (git org %s)",
            pr["pr_url"],
            pr["state"],
            new_state.value,
            git_org_id,
        )


def _update_pr_state(db, pr: dict[str, Any], new_state: PRState) -> None:
    """Write the new provider state onto the existing PR row."""
    row = db.query(PullRequest).filter(PullRequest.id == pr["pr_id"]).first()
    if row is None:
        return
    row.state = new_state.value
    db.commit()


def _decrypt(db_plugin: Any, encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return db_plugin.decrypt(encrypted)
    except Exception:
        logger.warning("fix PR reconcile: token decrypt failed", exc_info=True)
        return None


async def _fetch_state(
    provider: str,
    pr: dict[str, Any],
    *,
    app: Any,
    db_plugin: Any,
    org_token: str | None,
    base_url: str | None,
) -> PRState | None:
    """Ask the provider for one PR's current state. ``None`` = keep DB state."""
    if provider == "github":
        owner, _, repo_short = (pr["repo_name"] or "").partition("/")
        if not owner or not repo_short or not org_token:
            return None
        data = await app.github.fetch_pull_request(org_token, owner, repo_short, pr["pr_number"])
        return resolve_pr_state(data) if data is not None else None

    if provider == "gitlab":
        # Repo-level token override is the exception; otherwise the group token.
        token = _decrypt(db_plugin, pr["repo_token_encrypted"]) or org_token
        if not token or not pr["repo_external_id"]:
            return None
        data = await app.gitlab.fetch_merge_request(
            token, pr["repo_external_id"], pr["pr_number"], provider_url=base_url
        )
        return _resolve_gitlab_state(data) if data is not None else None

    return None
