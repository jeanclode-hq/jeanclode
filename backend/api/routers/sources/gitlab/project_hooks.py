"""Create or remove the Jeanclode webhook on individual GitLab projects.

For tenants on GitLab Free, where group webhooks are a Premium feature: an org
with ``manage_project_webhooks`` set gets a project-level webhook on every
project it owns, pointing back at ``/webhooks/gitlab``. Newly created projects
still need an instance system hook — without merge request events — to be
discovered in the first place; see backend/CLAUDE.md.

The sync consumers call ``ensure_project_hooks`` after upserting repo rows;
``remove_project_hooks`` runs from the teardown consumer when the setting is
turned off or the org is deleted.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# GitLab's per-instance API rate limits are easy to trip on a group with
# hundreds of projects — two calls each (list + create).
_CONCURRENCY = 5


async def ensure_project_hooks(
    gitlab_plugin: Any,
    *,
    access_token: str,
    provider_url: str,
    project_ids: list[str],
) -> None:
    """Ensure the Jeanclode webhook on each project. Best-effort, never raises."""
    if not project_ids:
        return

    hook_url = gitlab_plugin.get_effective_webhook_url()
    secret = gitlab_plugin.get_effective_webhook_secret()
    if not hook_url or not secret:
        logger.warning(
            "manage_project_webhooks is on but BACKEND_URL / the GitLab webhook "
            "secret is not configured — skipping project hook creation for %d project(s)",
            len(project_ids),
        )
        return

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    failures = 0

    async def _one(project_id: str) -> None:
        nonlocal failures
        async with semaphore:
            try:
                result = await gitlab_plugin.ensure_project_webhook(
                    access_token,
                    str(project_id),
                    hook_url=hook_url,
                    secret=secret,
                    provider_url=provider_url,
                )
                if result != "exists":
                    logger.info("Project hook %s for GitLab project %s", result, project_id)
            except Exception as e:
                failures += 1
                logger.warning(
                    "Failed to ensure project hook for GitLab project %s: %s", project_id, e
                )

    await asyncio.gather(*(_one(pid) for pid in project_ids))

    if failures:
        logger.warning(
            "Project hook setup: %d/%d project(s) failed — the token likely lacks "
            "'api' scope or Maintainer role",
            failures,
            len(project_ids),
        )


async def remove_project_hooks(
    gitlab_plugin: Any,
    *,
    projects: list[tuple[str, str, str]],
) -> None:
    """Delete the Jeanclode webhook from each project. Best-effort, never raises.

    ``projects`` is a list of ``(project_id, provider_url, access_token)`` —
    each project brings its own token because a project-token setup keeps the
    token on the repo, not the org.
    """
    if not projects:
        return

    hook_url = gitlab_plugin.get_effective_webhook_url()
    if not hook_url:
        logger.warning(
            "Cannot resolve the webhook URL — skipping project hook teardown for %d project(s)",
            len(projects),
        )
        return

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def _one(project_id: str, provider_url: str, access_token: str) -> None:
        async with semaphore:
            try:
                result = await gitlab_plugin.delete_project_webhook(
                    access_token,
                    str(project_id),
                    hook_url=hook_url,
                    provider_url=provider_url,
                )
                if result == "deleted":
                    logger.info("Project hook deleted for GitLab project %s", project_id)
            except Exception as e:
                logger.warning(
                    "Failed to delete project hook for GitLab project %s: %s", project_id, e
                )

    await asyncio.gather(*(_one(*project) for project in projects))
