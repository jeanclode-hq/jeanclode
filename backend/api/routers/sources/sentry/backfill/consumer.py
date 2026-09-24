"""FastStream consumer for Sentry issue backfill.

Fetches unresolved issues from the Sentry API for each Repository under an
org and creates Issue records with status=PENDING so the dispatcher can
pick them up. How much history is imported is bounded by the org's
``backfill`` scope setting, which can disable the import entirely.

Ref: ADR-004, ADR-005
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from faststream.redis import RedisRouter, StreamSub

from api.context import get_current_app
from api.database import (
    db_create_issue,
    db_get_issue_by_sentry_id,
    db_get_org_by_id,
    db_get_repositories_by_org,
)
from api.models.settings import BACKFILL_LOOKBACK_DAYS, BackfillScope, SentryOrgSettings
from api.plugins.sentry.models import SentryIssue
from api.routers.sources.sentry.backfill.schemas import BackfillIssuesMessage
from api.sse.publishers import publish_backfill_event

logger = logging.getLogger(__name__)

router = RedisRouter()

# Scope applied when an org's stored settings can't be read — import nothing
# rather than guess at a window the tenant never picked.
DEFAULT_BACKFILL_SCOPE = BackfillScope.NONE


def _is_recent(issue: SentryIssue, cutoff: datetime | None) -> bool:
    """Check if an issue was seen after the cutoff date.

    A ``None`` cutoff means no lower bound — every issue qualifies.
    """
    if cutoff is None:
        return True
    if issue.last_seen is None:
        return False
    last_seen = issue.last_seen
    # Ensure timezone-aware comparison
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return last_seen >= cutoff


def _resolve_scope(message: BackfillIssuesMessage, org_settings: dict | None) -> BackfillScope:
    """Decide the scope for this run.

    An explicit scope on the message wins — that's a manual trigger, where the
    user just asked for this import. Otherwise the org's stored preference
    decides, falling back to the default for orgs connected before the
    setting existed.
    """
    if message.scope is not None:
        return message.scope
    try:
        return SentryOrgSettings.model_validate(org_settings or {}).backfill
    except Exception:
        logger.warning("Invalid Sentry org settings, falling back to default backfill scope")
        return DEFAULT_BACKFILL_SCOPE


@router.subscriber(
    stream=StreamSub("jeanclode.events.sentry.backfill", group="jeanclode", consumer="worker-1")
)
async def backfill_issues(message: BackfillIssuesMessage) -> None:
    """Backfill recent unresolved issues from Sentry.

    The org's ``backfill`` setting bounds how much history is imported; a
    scope of ``none`` skips the run entirely so a large backlog doesn't
    swamp the pending pool at onboarding time. A manual trigger carries an
    explicit scope on the message and always runs.

    For each Repository under the org:
    1. Fetch unresolved issues via Sentry API (paginated)
    2. Filter to issues seen inside the scope's lookback window
    3. Create Issue records with status=PENDING
    4. Skip issues that already exist (by external_id)
    """
    org_id = UUID(message.org_id)
    org_slug = message.org_slug

    app = get_current_app()
    sentry_plugin = app.sentry
    db_plugin = app.database

    if not db_plugin or not sentry_plugin:
        logger.error("Required plugins not configured")
        return

    # Verify Organization exists and read per-org credentials + settings
    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            logger.error(f"Organization {org_id} not found")
            return
        workspace_id = org.workspace_id
        org_base_url = org.base_url
        org_auth_token_encrypted = org.auth_token_encrypted
        org_settings = org.settings

    scope = _resolve_scope(message, org_settings)
    if scope is BackfillScope.NONE:
        logger.info(f"Backfill disabled for org {org_slug} (scope=none), skipping")
        await publish_backfill_event(
            workspace_id=str(workspace_id) if workspace_id else "",
            action="skipped",
            payload={"org_id": str(org_id), "scope": scope.value},
        )
        return

    lookback_days = BACKFILL_LOOKBACK_DAYS[scope]
    logger.info(f"Starting issue backfill for org {org_slug} (scope={scope.value})")

    # Decrypt per-org auth token (falls back to plugin config if not set)
    auth_token = db_plugin.decrypt(org_auth_token_encrypted) if org_auth_token_encrypted else None

    # Get all projects for this org
    with db_plugin.session() as db:
        projects = db_get_repositories_by_org(db, org_id)

    if not projects:
        logger.info(f"No projects for org {org_slug}, skipping backfill")
        return

    cutoff = (
        datetime.now(UTC) - timedelta(days=lookback_days) if lookback_days is not None else None
    )
    total_created = 0
    total_skipped = 0

    for project in projects:
        project_slug = project.name
        project_pk = project.id

        logger.info(f"Backfilling issues for project {project_slug}")

        try:
            sentry_issues = await sentry_plugin.list_issues(
                org_slug,
                project.external_id,
                query="is:unresolved",
                auth_token=auth_token,
                base_url=org_base_url,
            )
        except Exception as e:
            logger.error(
                f"Failed to fetch issues for {project_slug}: {e}",
                exc_info=True,
            )
            continue

        recent_issues = [i for i in sentry_issues if _is_recent(i, cutoff)]
        window = f"within {lookback_days} days" if lookback_days is not None else "all time"
        logger.info(
            f"Project {project_slug}: {len(sentry_issues)} total, "
            f"{len(recent_issues)} in scope ({window})"
        )

        with db_plugin.session() as db:
            for sentry_issue in recent_issues:
                existing = db_get_issue_by_sentry_id(db, project_pk, sentry_issue.id)
                if existing:
                    total_skipped += 1
                    continue

                db_create_issue(
                    db=db,
                    repository_id=project_pk,
                    external_id=sentry_issue.id,
                    title=sentry_issue.title,
                    culprit=sentry_issue.culprit,
                    level=sentry_issue.level,
                    first_seen=sentry_issue.first_seen,
                    last_seen=sentry_issue.last_seen,
                    event_count=int(sentry_issue.count or 0),
                    issue_url=sentry_issue.permalink,
                )
                total_created += 1

        logger.info(f"Project {project_slug}: created {total_created} issues so far")

    logger.info(
        f"Backfill complete for org {org_slug} (scope={scope.value}): "
        f"{total_created} created, {total_skipped} skipped (already exist)"
    )

    await publish_backfill_event(
        workspace_id=str(workspace_id) if workspace_id else "",
        action="completed",
        payload={
            "org_id": str(org_id),
            "created": total_created,
            "skipped": total_skipped,
            "scope": scope.value,
        },
    )
