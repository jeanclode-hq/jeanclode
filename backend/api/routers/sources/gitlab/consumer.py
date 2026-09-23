"""FastStream consumer for GitLab group repository sync."""

import asyncio
import logging
from typing import Any
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import (
    db_create_issue,
    db_create_org,
    db_get_issue_by_sentry_id,
    db_get_org_by_external_id,
    db_get_org_by_id,
    db_get_repository_by_org_and_external_id,
    db_resolve_org_token,
    db_upsert_org_with_ancestors,
    db_upsert_pull_request,
    db_upsert_repository,
)
from api.database.organization import db_resolve_org_settings
from api.models.pull_requests import PRState
from api.routers.sources.gitlab.project_hooks import ensure_project_hooks, remove_project_hooks
from api.routers.sources.gitlab.schemas import (
    GitLabProjectHookTeardownMessage,
    GitLabSyncGroupRepositoriesMessage,
    GitLabSyncProjectRepositoryMessage,
)
from api.routers.sources.gitlab.utils import parse_gitlab_datetime
from api.sse.publishers import publish_sync_event

logger = logging.getLogger(__name__)

router = RedisRouter()

_GITLAB_STATE_MAP = {
    "merged": PRState.MERGED,
    "closed": PRState.CLOSED,
    "opened": PRState.OPEN,
}


async def _sync_mrs(
    gitlab_plugin,
    db_plugin,
    access_token: str,
    provider_url: str,
    repo_map: dict[str, tuple],
) -> int:
    """Sync open MRs for all repos. Returns total MRs synced."""
    total = 0
    for _ext_id, (db_repo_id, project_id) in repo_map.items():
        try:
            mrs = await gitlab_plugin.fetch_project_merge_requests(
                access_token, str(project_id), provider_url=provider_url, state="opened"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch MRs for project {project_id}: {e}")
            continue

        if not mrs:
            continue

        with db_plugin.session() as db:
            for mr_data in mrs:
                mr_state = _GITLAB_STATE_MAP.get(mr_data.get("state", ""), PRState.OPEN)
                mr_url = mr_data.get("web_url", "")
                last_commit = mr_data.get("sha") or (mr_data.get("diff_refs", {}) or {}).get(
                    "head_sha"
                )

                try:
                    db_upsert_pull_request(
                        db=db,
                        repository_id=db_repo_id,
                        pr_number=mr_data["iid"],
                        title=mr_data["title"],
                        author=mr_data.get("author", {}).get("username", "unknown"),
                        state=mr_state,
                        pr_url=mr_url,
                        head_branch=mr_data.get("source_branch", ""),
                        base_branch=mr_data.get("target_branch", ""),
                        external_pr_id=str(mr_data["id"]),
                        head_sha=last_commit,
                        created_at=parse_gitlab_datetime(mr_data.get("created_at")),
                    )
                    total += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to upsert MR !{mr_data.get('iid')} for project {project_id}: {e}"
                    )

    return total


async def _sync_issues(
    gitlab_plugin,
    db_plugin,
    access_token: str,
    provider_url: str,
    repo_map: dict[str, tuple],
) -> int:
    """Sync open issues for all repos. Returns total issues synced."""
    total = 0
    for _ext_id, (db_repo_id, project_id) in repo_map.items():
        try:
            issues = await gitlab_plugin.fetch_project_issues(
                access_token, str(project_id), provider_url=provider_url, state="opened"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch issues for project {project_id}: {e}")
            continue

        if not issues:
            continue

        with db_plugin.session() as db:
            for issue_data in issues:
                external_id = str(issue_data["iid"])

                existing = db_get_issue_by_sentry_id(db, db_repo_id, external_id)
                if existing:
                    continue

                gl_state = issue_data.get("state", "opened")
                status = "open" if gl_state == "opened" else "closed"

                try:
                    db_create_issue(
                        db=db,
                        repository_id=db_repo_id,
                        external_id=external_id,
                        title=issue_data.get("title", ""),
                        level="info",
                        status=status,
                        author=issue_data.get("author", {}).get("username"),
                        issue_url=issue_data.get("web_url"),
                        event_count=issue_data.get("user_notes_count", 0),
                        first_seen=parse_gitlab_datetime(issue_data.get("created_at")),
                        last_seen=parse_gitlab_datetime(issue_data.get("updated_at")),
                    )
                    total += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to create issue #{external_id} for project {project_id}: {e}"
                    )

    return total


async def _resolve_project_org(
    db_plugin: Any,
    gitlab_plugin: Any,
    access_token: str,
    provider_url: str,
    base_url: str | None,
    workspace_id: UUID,
    top_org_id: UUID,
    top_external_org_id: str,
    namespace_org_cache: dict[str, UUID],
    project_info: dict[str, Any],
) -> UUID:
    """Resolve the org for a project's immediate GitLab namespace.

    ``include_subgroups=True`` on the group-projects fetch returns projects
    from arbitrarily deep subgroups, not just the token's own group — each
    has to be linked to its own immediate namespace org (S1 in issue #171),
    not the top-level org the token was added for. Ancestor orgs between the
    namespace and the top-level org are auto-created as token-less
    placeholders, mirroring what ``_handle_project_token`` does for a single
    project token.
    """
    namespace = project_info.get("namespace") or {}
    namespace_id = str(namespace.get("id", top_external_org_id))

    if namespace_id == top_external_org_id:
        return top_org_id

    if namespace_id in namespace_org_cache:
        return namespace_org_cache[namespace_id]

    def _lookup(db: Session) -> UUID | None:
        org = db_get_org_by_external_id(db, namespace_id, provider="gitlab", base_url=base_url)
        return org.id if org else None

    existing_ns_org_id = await db_plugin.run_in_session(_lookup)
    if existing_ns_org_id:
        namespace_org_cache[namespace_id] = existing_ns_org_id
        return existing_ns_org_id

    # Fetched before the write session opens. Syncing a group runs this once
    # per unseen namespace, so holding a pooled connection across the round
    # trip would tie one up for the length of the whole sync.
    ancestors = await gitlab_plugin.fetch_group_ancestors(
        access_token, namespace_id, provider_url=provider_url
    )

    def _create(db: Session) -> UUID:
        parent_org_id, root_org_id = db_upsert_org_with_ancestors(
            db=db,
            workspace_id=workspace_id,
            provider="gitlab",
            ancestors=ancestors,
            base_url=base_url,
        )
        ns_org = db_create_org(
            db=db,
            workspace_id=workspace_id,
            name=namespace.get("name", ""),
            external_org_id=namespace_id,
            provider="gitlab",
            base_url=base_url,
            avatar_url=namespace.get("avatar_url"),
            parent_org_id=parent_org_id,
            root_org_id=root_org_id,
        )
        return ns_org.id

    ns_org_id = await db_plugin.run_in_session(_create)
    namespace_org_cache[namespace_id] = ns_org_id
    return ns_org_id


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.sync_group_repositories", group="jeanclode", consumer="worker-1"
    )
)
async def sync_gitlab_group_repositories(message: GitLabSyncGroupRepositoriesMessage) -> None:
    """Sync all repositories, open MRs, and open issues for a GitLab group.

    Runs in the background after a group token is added.
    Fetches all group projects from GitLab API, creates repos in DB,
    then syncs MRs and issues in parallel.
    """
    org_id = UUID(message.org_id)
    group_id = message.group_id

    logger.info(f"Starting repository sync for GitLab group {group_id}")

    app = get_current_app()
    gitlab_plugin = app.gitlab
    db_plugin = app.database

    if not db_plugin or not gitlab_plugin:
        logger.error("Required plugins not configured")
        return

    # Phase 1: DB reads — get org and decrypt token
    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return

        # A subgroup connected under a group token has no token of its own —
        # resolve upward so a re-sync works from any org in the hierarchy.
        resolved_token = db_resolve_org_token(db, org)
        if not resolved_token:
            logger.error(f"No GitLab access token found for organization {org_id}")
            return

        if not org.workspace_id:
            logger.error(f"Organization {org_id} has no workspace, cannot sync repositories")
            return

        access_token = db_plugin.decrypt(resolved_token)
        base_url = org.base_url
        provider_url = base_url or "https://gitlab.com"
        encrypted_token = org.auth_token_encrypted
        top_external_org_id = org.external_org_id
        workspace_uuid = org.workspace_id
        workspace_id = str(org.workspace_id) if org.workspace_id else None

    # Phase 2: External API calls — no DB session held
    try:
        await gitlab_plugin.ensure_group_labels(access_token, group_id, provider_url=provider_url)
    except Exception as e:
        logger.error(f"Failed to ensure trigger labels on group {group_id}: {e}")

    try:
        projects = await gitlab_plugin.fetch_group_projects(
            access_token, group_id, provider_url=provider_url
        )
    except Exception as e:
        logger.error(
            f"Failed to fetch projects for group {group_id}: {e}",
            exc_info=True,
        )
        return

    logger.info(f"Found {len(projects)} projects for group {group_id}")

    # Phase 3: DB writes — create repos
    from sqlalchemy.exc import IntegrityError

    repo_map: dict[str, tuple] = {}  # external_repo_id -> (db_repo_id, project_id)
    namespace_org_cache: dict[str, UUID] = {}

    # Namespace orgs first: resolving one can cost a GitLab round trip, and
    # the write session below must not be open across it.
    resolved: list[tuple[dict, UUID]] = []
    for project_info in projects:
        repo_name = project_info.get("path_with_namespace", project_info["name"])
        try:
            resolved.append(
                (
                    project_info,
                    await _resolve_project_org(
                        db_plugin,
                        gitlab_plugin,
                        access_token,
                        provider_url,
                        base_url,
                        workspace_uuid,
                        org_id,
                        top_external_org_id,
                        namespace_org_cache,
                        project_info,
                    ),
                )
            )
        except Exception as e:
            logger.warning(
                f"Failed to resolve namespace org for project {repo_name}: {e}",
                exc_info=True,
            )

    def _write_repos(db: Session) -> bool:
        """``False`` when the org vanished mid-sync and the batch was abandoned."""
        for project_info, target_org_id in resolved:
            external_repo_id = str(project_info["id"])
            repo_name = project_info.get("path_with_namespace", project_info["name"])

            project_avatar_url = project_info.get("avatar_url") or project_info.get(
                "namespace", {}
            ).get("avatar_url")

            try:
                # Upsert, not create-if-missing: a re-sync is how a project
                # added to the group after connection gets its row, and how a
                # renamed one stops pointing at its old path.
                repository = db_upsert_repository(
                    db=db,
                    org_id=target_org_id,
                    external_id=external_repo_id,
                    name=repo_name,
                    web_url=project_info["web_url"],
                    provider="gitlab",
                    provider_url=provider_url,
                    auth_token_encrypted=encrypted_token,
                    avatar_url=project_avatar_url,
                )
                repo_map[external_repo_id] = (repository.id, project_info["id"])
            except IntegrityError:
                db.rollback()
                return False
        return True

    if not await db_plugin.run_in_session(_write_repos):
        logger.warning(f"Organization {org_id} was deleted during sync, aborting")
        return

    logger.info(f"Successfully synced repositories for group {group_id}")

    # Publish SSE event after repo sync
    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="repos_synced",
            payload={"org_id": str(org_id), "repo_count": len(projects)},
        )

    # GitLab Free has no group webhooks — create a project hook on each project
    # when the org opted in.
    with db_plugin.session() as db:
        settings_org = db_get_org_by_id(db, org_id)
        manage_hooks = bool(
            settings_org
            and db_resolve_org_settings(db, settings_org).get("manage_project_webhooks")
        )
    if manage_hooks:
        await ensure_project_hooks(
            gitlab_plugin,
            access_token=access_token,
            provider_url=provider_url,
            project_ids=[str(project_id) for (_repo_id, project_id) in repo_map.values()],
        )

    # Phase 4: Sync open MRs and issues in parallel
    total_mrs, total_issues = await asyncio.gather(
        _sync_mrs(gitlab_plugin, db_plugin, access_token, provider_url, repo_map),
        _sync_issues(gitlab_plugin, db_plugin, access_token, provider_url, repo_map),
    )

    if total_mrs:
        logger.info(f"Synced {total_mrs} open MRs across {len(repo_map)} repositories")
    if total_issues:
        logger.info(f"Synced {total_issues} open issues across {len(repo_map)} repositories")

    # Publish SSE events
    if workspace_id:
        if total_mrs:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="prs_synced",
                payload={"org_id": str(org_id), "pr_count": total_mrs},
            )
        if total_issues:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="issues_synced",
                payload={"org_id": str(org_id), "issue_count": total_issues},
            )


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.sync_project_repository", group="jeanclode", consumer="worker-1"
    )
)
async def sync_gitlab_project_repository(message: GitLabSyncProjectRepositoryMessage) -> None:
    """Sync open MRs and issues for a single GitLab project.

    Runs in the background after a project token is added.
    """
    org_id = UUID(message.org_id)
    project_id = message.project_id

    logger.info(f"Starting repository sync for GitLab project {project_id}")

    app = get_current_app()
    gitlab_plugin = app.gitlab
    db_plugin = app.database

    if not db_plugin or not gitlab_plugin:
        logger.error("Required plugins not configured")
        return

    with db_plugin.session() as db:
        repo = db_get_repository_by_org_and_external_id(db, org_id, project_id)
        if not repo:
            logger.error(f"Repository for project {project_id} not found")
            return

        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return

        # Resolve token: repo-level first, then the org chain (a repo inherits
        # after a group upgrade, and a subgroup org holds no token of its own)
        encrypted = repo.auth_token_encrypted or db_resolve_org_token(db, org)
        if not encrypted:
            logger.error(f"No token found for project {project_id}")
            return

        access_token = db_plugin.decrypt(encrypted)
        provider_url = repo.provider_url or org.base_url or "https://gitlab.com"
        workspace_id = str(org.workspace_id) if org.workspace_id else None
        db_repo_id = repo.id
        manage_hooks = bool(db_resolve_org_settings(db, org).get("manage_project_webhooks"))

    if manage_hooks:
        await ensure_project_hooks(
            gitlab_plugin,
            access_token=access_token,
            provider_url=provider_url,
            project_ids=[project_id],
        )

    repo_map = {project_id: (db_repo_id, int(project_id))}

    total_mrs, total_issues = await asyncio.gather(
        _sync_mrs(gitlab_plugin, db_plugin, access_token, provider_url, repo_map),
        _sync_issues(gitlab_plugin, db_plugin, access_token, provider_url, repo_map),
    )

    if total_mrs:
        logger.info(f"Synced {total_mrs} open MRs for project {project_id}")
    if total_issues:
        logger.info(f"Synced {total_issues} open issues for project {project_id}")

    if workspace_id:
        if total_mrs:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="prs_synced",
                payload={"org_id": str(org_id), "pr_count": total_mrs},
            )
        if total_issues:
            await publish_sync_event(
                workspace_id=workspace_id,
                action="issues_synced",
                payload={"org_id": str(org_id), "issue_count": total_issues},
            )


@router.subscriber(
    stream=StreamSub(
        "jeanclode.events.gitlab.project_hook_teardown", group="jeanclode", consumer="worker-1"
    )
)
async def teardown_gitlab_project_hooks(message: GitLabProjectHookTeardownMessage) -> None:
    """Delete Jeanclode project webhooks after the setting is turned off or the org
    is removed. The message is self-contained — the org rows may already be gone."""
    app = get_current_app()
    gitlab_plugin = app.gitlab
    db_plugin = app.database
    if not gitlab_plugin or not db_plugin:
        logger.error("Required plugins not configured")
        return

    projects: list[tuple[str, str, str]] = []
    for item in message.projects:
        try:
            projects.append(
                (item.project_id, item.provider_url, db_plugin.decrypt(item.encrypted_token))
            )
        except Exception as e:
            logger.error(f"Could not decrypt token for GitLab project {item.project_id}: {e}")

    await remove_project_hooks(gitlab_plugin, projects=projects)
