"""FastStream consumer for GitHub installation sync operations."""

import asyncio
import logging

from faststream.redis import RedisRouter, StreamSub
from sqlalchemy.exc import IntegrityError

from api.context import get_current_app
from api.database import (
    db_create_issue,
    db_get_issue_by_sentry_id,
    db_get_org_by_installation_id,
    db_upsert_pull_request,
    db_upsert_repository,
)
from api.routers.webhooks.github.schemas import (
    GitHubSyncInstallationMessage,
    GitHubSyncRepositoriesMessage,
)
from api.routers.webhooks.github.utils import parse_github_datetime, resolve_pr_state
from api.sse.publishers import publish_sync_event

logger = logging.getLogger(__name__)

router = RedisRouter()


async def _ensure_repo_labels(
    github_plugin, installation_token: str, repo_map: dict[str, tuple]
) -> None:
    """Best-effort: create the jeanclode:* labels on each newly-synced repo."""

    async def _one(full_name: str) -> None:
        try:
            await github_plugin.ensure_repo_labels(installation_token, full_name)
        except Exception as e:
            logger.warning(f"Failed to ensure trigger labels on {full_name}: {e}")

    full_names = [full_name for _, full_name in repo_map.values() if full_name]
    if full_names:
        await asyncio.gather(*(_one(full_name) for full_name in full_names))


async def _sync_prs(
    github_plugin,
    db_plugin,
    installation_token: str,
    repo_map: dict[str, tuple],
) -> int:
    """Sync open PRs for all repos. Returns total PRs synced."""
    total = 0
    for _ext_id, (db_repo_id, full_name) in repo_map.items():
        if not full_name or "/" not in full_name:
            continue

        owner, repo_name = full_name.split("/", 1)

        try:
            prs = await github_plugin.fetch_repository_pull_requests(
                installation_token, owner, repo_name, state="open"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch PRs for {full_name}: {e}")
            continue

        if not prs:
            continue

        with db_plugin.session() as db:
            for pr_data in prs:
                try:
                    db_upsert_pull_request(
                        db=db,
                        repository_id=db_repo_id,
                        pr_number=pr_data["number"],
                        title=pr_data["title"],
                        author=pr_data["user"]["login"],
                        state=resolve_pr_state(pr_data),
                        pr_url=pr_data["html_url"],
                        head_branch=pr_data["head"]["ref"],
                        base_branch=pr_data["base"]["ref"],
                        external_pr_id=str(pr_data["id"]),
                        head_sha=pr_data["head"].get("sha"),
                        created_at=parse_github_datetime(pr_data.get("created_at")),
                    )
                    total += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to upsert PR #{pr_data.get('number')} for {full_name}: {e}"
                    )

    return total


async def _sync_issues(
    github_plugin,
    db_plugin,
    installation_token: str,
    repo_map: dict[str, tuple],
) -> int:
    """Sync open issues for all repos. Returns total issues synced."""
    total = 0
    for _ext_id, (db_repo_id, full_name) in repo_map.items():
        if not full_name or "/" not in full_name:
            continue

        owner, repo_name = full_name.split("/", 1)

        try:
            issues = await github_plugin.fetch_repository_issues(
                installation_token, owner, repo_name, state="open"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch issues for {full_name}: {e}")
            continue

        if not issues:
            continue

        with db_plugin.session() as db:
            for issue_data in issues:
                external_id = str(issue_data["number"])

                # Skip if already exists
                existing = db_get_issue_by_sentry_id(db, db_repo_id, external_id)
                if existing:
                    continue

                try:
                    db_create_issue(
                        db=db,
                        repository_id=db_repo_id,
                        external_id=external_id,
                        title=issue_data.get("title", ""),
                        level="info",
                        status=issue_data.get("state", "open"),
                        author=issue_data.get("user", {}).get("login"),
                        issue_url=issue_data.get("html_url"),
                        event_count=issue_data.get("comments", 0),
                        first_seen=parse_github_datetime(issue_data.get("created_at")),
                        last_seen=parse_github_datetime(issue_data.get("updated_at")),
                    )
                    total += 1
                except Exception as e:
                    logger.warning(f"Failed to create issue #{external_id} for {full_name}: {e}")

    return total


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.github.sync_installation", group="jeanclode", consumer="worker-1"
    )
)
async def sync_installation_repositories(message: GitHubSyncInstallationMessage) -> None:
    """Sync all repositories, open PRs, and open issues for a GitHub App installation.

    Runs in the background after an installation.created webhook.
    Fetches all repos via GitHub API, creates them in DB, then syncs PRs and issues in parallel.
    """
    installation_id = message.installation_id

    logger.info(f"Starting repository sync for installation {installation_id}")

    app = get_current_app()
    github_plugin = app.github
    if not github_plugin or not app.database:
        logger.error("GitHub or database plugin not configured")
        return

    # Phase 1: DB reads — find org
    with app.database.session() as db:
        org = db_get_org_by_installation_id(db, installation_id)
        if not org:
            logger.error(f"Organization not found for installation {installation_id}")
            return
        org_id = org.id
        workspace_id = str(org.workspace_id) if org.workspace_id else None

    # Phase 2: External API calls — no DB session held
    try:
        installation_token = await github_plugin.get_installation_access_token(installation_id)
        repos = await github_plugin.fetch_installation_repositories(installation_token)
    except Exception as e:
        logger.error(
            f"Failed to sync repositories for installation {installation_id}: {e}",
            exc_info=True,
        )
        return

    logger.info(f"Found {len(repos)} repositories for installation {installation_id}")

    # Phase 3: DB writes — create repos
    repo_map: dict[str, tuple] = {}  # external_repo_id -> (db_repo_id, full_name)

    with app.database.session() as db:
        for repo_info in repos:
            external_repo_id = str(repo_info["id"])

            try:
                # Upsert, not create-if-missing: a re-sync is also how a row
                # written from a thin webhook payload gets its real name,
                # URL and avatar back.
                repository = db_upsert_repository(
                    db=db,
                    org_id=org_id,
                    external_id=external_repo_id,
                    name=repo_info["name"],
                    web_url=repo_info["html_url"],
                    provider="github",
                    provider_url="https://github.com",
                    avatar_url=repo_info.get("owner", {}).get("avatar_url"),
                )
                repo_map[external_repo_id] = (repository.id, repo_info.get("full_name", ""))
            except IntegrityError:
                db.rollback()
                logger.warning(
                    f"Organization for installation {installation_id} was deleted during sync, aborting"
                )
                return

    logger.info(f"Successfully synced {len(repos)} repositories for installation {installation_id}")

    await _ensure_repo_labels(github_plugin, installation_token, repo_map)

    # Publish SSE event after repo sync so frontend updates repo_count immediately
    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="repos_synced",
            payload={"org_id": str(org_id), "repo_count": len(repos)},
        )

    # Phase 4: Sync open PRs and issues in parallel
    total_prs, total_issues = await asyncio.gather(
        _sync_prs(github_plugin, app.database, installation_token, repo_map),
        _sync_issues(github_plugin, app.database, installation_token, repo_map),
    )

    if total_prs:
        logger.info(f"Synced {total_prs} open PRs across {len(repo_map)} repositories")
    if total_issues:
        logger.info(f"Synced {total_issues} open issues across {len(repo_map)} repositories")

    # Publish SSE events
    if workspace_id:
        if total_prs:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="prs_synced",
                payload={"org_id": str(org_id), "pr_count": total_prs},
            )
        if total_issues:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="issues_synced",
                payload={"org_id": str(org_id), "issue_count": total_issues},
            )


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.github.sync_repositories", group="jeanclode", consumer="worker-1"
    )
)
async def sync_added_repositories(message: GitHubSyncRepositoriesMessage) -> None:
    """Complete the rows for repos that appeared after the installation sync.

    ``installation_repositories.added`` and ``repository.created`` create the
    row from a payload that carries neither the owner avatar nor (for the
    former) the repo URL, and never any PRs or issues. This refetches each
    repo from the API and runs the same open-PR/open-issue backfill the
    installation sync does.
    """
    installation_id = message.installation_id

    app = get_current_app()
    github_plugin = app.github
    if not github_plugin or not app.database:
        logger.error("GitHub or database plugin not configured")
        return

    with app.database.session() as db:
        org = db_get_org_by_installation_id(db, installation_id)
        if not org:
            logger.error(f"Organization not found for installation {installation_id}")
            return
        org_id = org.id
        workspace_id = str(org.workspace_id) if org.workspace_id else None

    try:
        installation_token = await github_plugin.get_installation_access_token(installation_id)
    except Exception as e:
        logger.error(
            f"Failed to mint installation token for {installation_id}: {e}",
            exc_info=True,
        )
        return

    repo_map: dict[str, tuple] = {}
    for external_repo_id in message.external_repo_ids:
        try:
            repo_info = await github_plugin.fetch_repository_by_id(
                installation_token, external_repo_id
            )
        except Exception as e:
            logger.warning(f"Failed to fetch repository {external_repo_id}: {e}")
            continue

        if not repo_info:
            continue

        with app.database.session() as db:
            try:
                repository = db_upsert_repository(
                    db=db,
                    org_id=org_id,
                    external_id=external_repo_id,
                    name=repo_info["name"],
                    web_url=repo_info["html_url"],
                    provider="github",
                    provider_url="https://github.com",
                    avatar_url=repo_info.get("owner", {}).get("avatar_url"),
                )
            except IntegrityError:
                db.rollback()
                logger.warning(f"Organization {org_id} was deleted during sync, aborting")
                return
            repo_map[external_repo_id] = (repository.id, repo_info.get("full_name", ""))

    if not repo_map:
        return

    await _ensure_repo_labels(github_plugin, installation_token, repo_map)

    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="repos_synced",
            payload={"org_id": str(org_id), "repo_count": len(repo_map)},
        )

    total_prs, total_issues = await asyncio.gather(
        _sync_prs(github_plugin, app.database, installation_token, repo_map),
        _sync_issues(github_plugin, app.database, installation_token, repo_map),
    )

    logger.info(
        f"Backfilled {len(repo_map)} repositories for installation {installation_id} "
        f"({total_prs} PRs, {total_issues} issues)"
    )

    if workspace_id:
        if total_prs:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="prs_synced",
                payload={"org_id": str(org_id), "pr_count": total_prs},
            )
        if total_issues:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="issues_synced",
                payload={"org_id": str(org_id), "issue_count": total_issues},
            )
