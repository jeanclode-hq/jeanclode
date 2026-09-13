"""GitHub installation webhook handlers."""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.database import (
    db_create_org,
    db_delete_org,
    db_delete_repository,
    db_get_org_by_external_id,
    db_get_org_by_installation_id,
    db_get_repository_by_external_id,
    db_update_org,
    db_upsert_repository,
    run_in_session,
)
from api.models.organizations import Organization
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.orgs.schemas import SyncOrgMembersMessage

from ..utils import WebhookResponse
from .schemas import (
    GitHubAppInstallationEvent,
    GitHubAppInstallationRepositoriesEvent,
    GitHubRepositoryEvent,
    GitHubSyncInstallationMessage,
    GitHubSyncRepositoriesMessage,
)

logger = logging.getLogger(__name__)


def upsert_repository_from_payload(
    db: Session,
    org: Organization,
    repo_info: dict,
) -> None:
    """Create or refresh a repository row from a webhook repository object.

    ``installation_repositories`` entries are thin — id, name, full_name,
    private and nothing else — so ``html_url`` and the owner avatar have to be
    derived rather than read. The background sync repairs both from the API
    afterwards; this keeps the row usable in the meantime.
    """
    full_name = repo_info.get("full_name") or f"{org.name}/{repo_info['name']}"
    web_url = repo_info.get("html_url") or f"https://github.com/{full_name}"
    avatar_url = (repo_info.get("owner") or {}).get("avatar_url") or org.avatar_url

    db_upsert_repository(
        db=db,
        org_id=org.id,
        external_id=str(repo_info["id"]),
        name=repo_info["name"],
        web_url=web_url,
        provider="github",
        provider_url="https://github.com",
        avatar_url=avatar_url,
    )


async def queue_repository_backfill(
    installation_id: str,
    external_repo_ids: list[str],
) -> None:
    """Queue the PR/issue backfill for repos that appeared after install."""
    if not external_repo_ids:
        return

    try:
        broker = get_faststream_broker()
        await broker.publish(
            GitHubSyncRepositoriesMessage(
                installation_id=installation_id,
                external_repo_ids=external_repo_ids,
            ),
            stream="jeanclode.events.github.sync_repositories",
            maxlen=STREAM_MAXLEN,
        )
    except Exception as e:
        logger.error(
            f"Failed to queue repo backfill for installation {installation_id}: {e}",
            exc_info=True,
        )


async def handle_installation_created(event: GitHubAppInstallationEvent) -> WebhookResponse:
    """Handle installation.created event — create org, queue repo sync."""
    installation = event.installation
    sender = event.sender

    if "account" not in installation:
        raise HTTPException(status_code=400, detail="Missing account data in installation payload")

    account = installation["account"]
    installation_id = str(installation["id"])
    external_org_id = str(account["id"])
    org_name = account["login"]
    avatar_url = account.get("avatar_url")

    def _persist(db: Session) -> tuple[str, str] | None:
        """Create or refresh the org; ``None`` when this install is a replay."""
        # Idempotent: check by installation_id first (same install replayed)
        if db_get_org_by_installation_id(db, installation_id):
            return None

        # Reinstall: check by external_org_id (constant across reinstalls)
        existing_org = db_get_org_by_external_id(db, external_org_id, provider="github")
        if existing_org:
            db_update_org(
                db,
                existing_org.id,
                installation_id=installation_id,
                avatar_url=avatar_url,
            )
            org = existing_org
        else:
            # New install — create orphaned org (no workspace yet).
            # The frontend will claim it via POST /organizations/claim after redirect.
            org = db_create_org(
                db=db,
                workspace_id=None,
                name=org_name,
                external_org_id=external_org_id,
                provider="github",
                installation_id=installation_id,
                base_url="https://github.com",
                avatar_url=avatar_url,
            )
        return str(org.id), org.name

    persisted = await run_in_session(_persist)
    if persisted is None:
        return WebhookResponse(
            message=f"Organization {org_name} already exists",
            processed=True,
        )
    org_id, created_org_name = persisted

    # Queue background sync for repositories
    sender_github_id = str(sender["id"])
    try:
        broker = get_faststream_broker()
        sync_message = GitHubSyncInstallationMessage(
            installation_id=installation_id,
            sender_github_id=sender_github_id,
        )
        await broker.publish(
            sync_message,
            stream="jeanclode.events.github.sync_installation",
            maxlen=STREAM_MAXLEN,
        )
        logger.info(f"Queued repository sync for installation {installation_id}")
    except Exception as e:
        logger.error(
            f"Failed to queue repository sync for installation {installation_id}: {e}",
            exc_info=True,
        )

    # Queue background member pre-population
    try:
        broker = get_faststream_broker()
        await broker.publish(
            SyncOrgMembersMessage(org_id=org_id, provider="github"),
            stream="jeanclode.events.orgs.sync_members",
            maxlen=STREAM_MAXLEN,
        )
        logger.info(f"Queued member sync for installation {installation_id}")
    except Exception as e:
        logger.error(
            f"Failed to queue member sync for installation {installation_id}: {e}",
            exc_info=True,
        )

    return WebhookResponse(
        message=f"Organization {created_org_name} created, syncing repositories in background",
        processed=True,
    )


def handle_installation_deleted(
    event: GitHubAppInstallationEvent,
    db: Session,
) -> WebhookResponse:
    """Handle installation.deleted event — delete org."""
    installation = event.installation
    installation_id = str(installation["id"])

    org = db_get_org_by_installation_id(db, installation_id)
    if not org:
        return WebhookResponse(
            message="Organization not found (may have been deleted already)",
            processed=True,
        )

    org_name = org.name
    db_delete_org(db, installation_id)

    return WebhookResponse(
        message=f"Organization {org_name} deleted successfully",
        processed=True,
    )


async def handle_repositories_added(
    event: GitHubAppInstallationRepositoriesEvent,
) -> WebhookResponse:
    """Handle installation_repositories.added event — add repos directly."""
    installation = event.installation
    installation_id = str(installation["id"])
    repositories_added = event.repositories_added or []

    def _persist(db: Session) -> tuple[list[str], str]:
        org = db_get_org_by_installation_id(db, installation_id)
        if not org:
            raise HTTPException(
                status_code=404,
                detail=f"Organization not found for installation {installation_id}",
            )

        added_ids: list[str] = []
        for repo_info in repositories_added:
            upsert_repository_from_payload(db, org, repo_info)
            added_ids.append(str(repo_info["id"]))
        return added_ids, org.name

    added_ids, org_name = await run_in_session(_persist)

    # The event carries no PRs or issues, so a repo that already had open ones
    # would otherwise start out looking empty.
    await queue_repository_backfill(installation_id, added_ids)

    return WebhookResponse(
        message=f"Added {len(added_ids)} repositories to organization {org_name}",
        processed=True,
    )


def handle_repositories_removed(
    event: GitHubAppInstallationRepositoriesEvent,
    db: Session,
) -> WebhookResponse:
    """Handle installation_repositories.removed event — remove repos."""
    repositories_removed = event.repositories_removed or []

    removed_count = 0
    for repo_info in repositories_removed:
        external_repo_id = str(repo_info["id"])
        if db_delete_repository(db, external_repo_id):
            removed_count += 1

    return WebhookResponse(
        message=f"Removed {removed_count} repositories from installation",
        processed=True,
    )


async def handle_repository_event(event: GitHubRepositoryEvent) -> WebhookResponse:
    """Handle the ``repository`` event — created, deleted, renamed, transferred.

    An installation scoped to all repositories does not reliably announce a
    newly created repo through ``installation_repositories``, and renames only
    ever arrive here, so this is what keeps the row honest over time.
    """
    repo_info = event.repository
    external_repo_id = str(repo_info.get("id", ""))
    if not external_repo_id:
        return WebhookResponse(message="Missing repository ID in payload", processed=False)

    if event.action == "deleted":
        deleted = await run_in_session(lambda db: db_delete_repository(db, external_repo_id))
        return WebhookResponse(
            message=(
                f"Removed repository {repo_info.get('full_name', external_repo_id)}"
                if deleted
                else "Repository not tracked"
            ),
            processed=True,
        )

    if event.action not in ("created", "renamed", "transferred", "publicized", "privatized"):
        return WebhookResponse(
            message=f"Repository action '{event.action}' not processed",
            processed=False,
        )

    installation = event.installation or {}
    installation_id = str(installation.get("id", ""))
    if not installation_id:
        return WebhookResponse(message="Event carries no installation", processed=False)

    def _persist(db: Session) -> bool | None:
        """``True`` when the repo is new and needs a backfill; ``None`` when unknown org."""
        org = db_get_org_by_installation_id(db, installation_id)
        if not org:
            return None

        existing = db_get_repository_by_external_id(db, external_repo_id)

        # A transfer between two connected orgs would otherwise leave a stale row
        # under the old one, shadowing the real repo in every by-external-id lookup.
        if existing and existing.org_id != org.id:
            existing.org_id = org.id
            db.commit()

        upsert_repository_from_payload(db, org, repo_info)
        return existing is None

    is_new = await run_in_session(_persist)
    if is_new is None:
        return WebhookResponse(
            message=f"Organization not found for installation {installation_id}",
            processed=False,
        )

    # A repo we already track has its PRs and issues arriving by webhook; only
    # one we are seeing for the first time needs the backfill.
    if is_new:
        await queue_repository_backfill(installation_id, [external_repo_id])

    return WebhookResponse(
        message=f"Repository {repo_info.get('full_name', external_repo_id)} {event.action}",
        processed=True,
    )
