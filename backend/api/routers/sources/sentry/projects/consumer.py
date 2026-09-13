"""FastStream consumer for project-to-repo mapping resolution."""

import logging
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_get_org_by_id,
    db_get_orgs_by_workspace,
    db_get_repositories_by_org,
    db_get_unmapped_repositories,
    db_update_org,
    db_update_repository_mapping,
)
from api.models.organizations import OnboardingStep
from api.models.repositories import MappingMethod
from api.routers.sources.sentry.projects.schemas import ResolveMappingsMessage
from api.routers.sources.sentry.projects.utils import (
    resolve_via_code_mappings,
    resolve_via_fuzzy_names,
)
from api.sse.publishers import publish_mapping_event

logger = logging.getLogger(__name__)

router = RedisRouter()


async def _finalize(
    db_plugin, workspace_id: UUID | None, git_org_ids: list[UUID], mapped: int, total: int
) -> None:
    """Advance each git org's onboarding step (if still pre-mapping) and emit one SSE event."""
    with db_plugin.session() as db:
        for git_org_id in git_org_ids:
            org = db_get_org_by_id(db, git_org_id)
            if not org:
                continue
            if org.onboarding_step not in (
                OnboardingStep.MAPPING.value,
                OnboardingStep.COMPLETE.value,
            ):
                db_update_org(db, git_org_id, onboarding_step=OnboardingStep.MAPPING.value)

    await publish_mapping_event(
        workspace_id=str(workspace_id) if workspace_id else "",
        action="resolved",
        payload={"mapped": mapped, "total": total},
    )


@router.subscriber(
    stream=StreamSub("jeanclode.events.mapping.resolve", group="jeanclode", consumer="worker-1")
)
async def resolve_project_mappings(message: ResolveMappingsMessage) -> None:
    """Resolve project-to-repo mappings across all git orgs in the workspace.

    1. Loads the union of repositories from every GitHub/GitLab org in the
       Sentry org's workspace
    2. Fetches Sentry code mappings once
    3. Matches code-mapping repo URLs against Repository URLs
    4. Falls back to fuzzy name matching across the combined pool
    5. Skips manually-mapped projects
    6. Advances onboarding step for each git org
    7. Emits a single SSE event
    """
    sentry_org_id = UUID(message.sentry_org_id)

    app = get_current_app()
    sentry_plugin = app.sentry
    db_plugin = app.database

    if not db_plugin:
        logger.error("Database plugin not configured")
        return

    with db_plugin.session() as db:
        sentry_org = db_get_org_by_id(db, sentry_org_id)
        if not sentry_org:
            logger.error(f"Sentry organization {sentry_org_id} not found")
            return
        org_slug = sentry_org.external_org_id
        workspace_id = sentry_org.workspace_id
        org_base_url = sentry_org.base_url
        org_auth_token_encrypted = sentry_org.auth_token_encrypted

        if not workspace_id:
            logger.error(f"Sentry org {sentry_org_id} has no workspace")
            return

        # require_token=False: a GitLab subgroup org holds its projects but no
        # token of its own (the group above it has that), and those projects
        # are most of what there is to map — 266 of 333 in one real workspace.
        git_orgs = db_get_orgs_by_workspace(db, workspace_id, provider="github")
        git_orgs += db_get_orgs_by_workspace(
            db, workspace_id, provider="gitlab", require_token=False
        )
        git_org_ids = [o.id for o in git_orgs]

        projects = db_get_unmapped_repositories(db, sentry_org_id)
        repositories = [r for o in git_orgs for r in db_get_repositories_by_org(db, o.id)]

    logger.info(f"Starting mapping resolution for {org_slug} ({len(git_orgs)} git orgs)")

    if not projects or not repositories:
        if not projects:
            logger.info(f"No projects to resolve for {org_slug}")
        else:
            logger.warning(f"No repositories found across git orgs for {org_slug}")
        await _finalize(db_plugin, workspace_id, git_org_ids, mapped=0, total=len(projects))
        return

    # Decrypt per-org auth token (falls back to plugin config if not set)
    auth_token = db_plugin.decrypt(org_auth_token_encrypted) if org_auth_token_encrypted else None

    # Step 1: code mapping resolution
    code_mapping_matches = {}
    if sentry_plugin:
        try:
            code_mappings = await sentry_plugin.list_code_mappings(
                org_slug, auth_token=auth_token, base_url=org_base_url
            )
            code_mapping_matches = resolve_via_code_mappings(projects, repositories, code_mappings)
            logger.info(
                f"Code mapping resolution: {len(code_mapping_matches)}/{len(projects)} matched"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch code mappings for {org_slug}: {e}")

    # Step 2: fuzzy name matching for remaining
    fuzzy_matches = resolve_via_fuzzy_names(projects, repositories, code_mapping_matches)
    logger.info(f"Fuzzy name resolution: {len(fuzzy_matches)}/{len(projects)} matched")

    # Apply all mappings
    total_mapped = 0
    with db_plugin.session() as db:
        for project in projects:
            if project.id in code_mapping_matches:
                db_update_repository_mapping(
                    db,
                    project.id,
                    code_mapping_matches[project.id],
                    MappingMethod.CODE_MAPPING.value,
                )
                total_mapped += 1
            elif project.id in fuzzy_matches:
                db_update_repository_mapping(
                    db, project.id, fuzzy_matches[project.id], MappingMethod.FUZZY.value
                )
                total_mapped += 1

    logger.info(
        f"Mapping resolution complete for {org_slug}: "
        f"{total_mapped}/{len(projects)} projects mapped"
    )

    await _finalize(db_plugin, workspace_id, git_org_ids, mapped=total_mapped, total=len(projects))

    # No backfill chained here. Issues are stored by Sentry project and the
    # repo association is resolved at processing time (ADR-005), so backfill
    # doesn't depend on mappings existing — the project sync consumer already
    # queues it once per connect. Queueing again here just re-walked the whole
    # Sentry API for issues that were already imported.
