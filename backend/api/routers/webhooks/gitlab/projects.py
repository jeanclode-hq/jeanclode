"""Materializing a GitLab project announced by a hook.

Shared by ``group_hook`` and ``system_hook``: both carry the same
``project_create`` / ``project_destroy`` payload, which holds ids and nothing
else — no ``web_url``, no namespace path — so the project is refetched from
the API with the token of the org that claims it.

Neither hook says which connected group it belongs to, and a system hook fires
for every project on the instance, so ownership is established here rather than
assumed: the project is fetched with each connected group's token, and kept
only if its namespace chain actually contains that group.
"""

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.database import (
    db_delete_repository,
    db_get_org_by_id,
    db_get_repository_by_external_id,
    db_list_gitlab_orgs,
    db_resolve_org_token,
    db_upsert_repository,
)
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker
from api.routers.sources.gitlab.consumer import _resolve_project_org
from api.routers.sources.gitlab.schemas import GitLabSyncProjectRepositoryMessage
from api.sse.publishers import publish_sync_event

from ..utils import WebhookResponse

logger = logging.getLogger(__name__)

DEFAULT_GITLAB_URL = "https://gitlab.com"

# Everything but destroy resolves to the same thing: make the row match the
# project as it is now, wherever it now lives.
ENSURE_EVENTS = frozenset(
    {"project_create", "project_rename", "project_transfer", "project_update"}
)
REMOVE_EVENTS = frozenset({"project_destroy"})
PROJECT_EVENTS = ENSURE_EVENTS | REMOVE_EVENTS


class Candidate(BaseModel):
    """A connected org that might own the project a hook announced."""

    org_id: UUID = Field(description="Organization database ID")
    external_org_id: str = Field(description="GitLab group ID the org maps to")
    base_url: str | None = Field(default=None, description="GitLab instance URL")
    workspace_id: UUID = Field(description="Workspace the org belongs to")
    access_token: str = Field(description="Decrypted token covering the org")

    @property
    def provider_url(self) -> str:
        return self.base_url or DEFAULT_GITLAB_URL


def same_instance(base_url: str | None, instance_url: str | None) -> bool:
    """Whether an org's instance matches the one the hook came from."""
    if not instance_url:
        return True
    return (base_url or DEFAULT_GITLAB_URL).rstrip("/") == instance_url.rstrip("/")


def _collect_candidates(
    db: Any,
    db_plugin: Any,
    namespace_id: str,
    instance_url: str | None,
) -> list[Candidate]:
    """Orgs that could own this project, most likely first.

    The project's own namespace comes first when it is already connected;
    otherwise every tokened GitLab org on the instance is a candidate, since a
    group token also covers projects in subgroups that were never connected on
    their own.
    """
    orgs = [org for org in db_list_gitlab_orgs(db) if same_instance(org.base_url, instance_url)]
    orgs.sort(key=lambda org: org.external_org_id != namespace_id)

    candidates: list[Candidate] = []
    seen_tokens: set[tuple[str, str]] = set()
    for org in orgs:
        if not org.workspace_id:
            continue
        encrypted = db_resolve_org_token(db, org)
        if not encrypted:
            continue
        # Two orgs under the same connected group share one token; probing the
        # API once per org would repeat the same call for no new answer.
        fingerprint = (encrypted, org.base_url or DEFAULT_GITLAB_URL)
        if fingerprint in seen_tokens and org.external_org_id != namespace_id:
            continue
        seen_tokens.add(fingerprint)
        candidates.append(
            Candidate(
                org_id=org.id,
                external_org_id=org.external_org_id,
                base_url=org.base_url,
                workspace_id=org.workspace_id,
                access_token=db_plugin.decrypt(encrypted),
            )
        )
    return candidates


async def _claim_project(
    gitlab_plugin: Any,
    candidate: Candidate,
    project_id: str,
) -> dict[str, Any] | None:
    """Fetch the project with a candidate's token, if that token owns it.

    A group token can read public projects anywhere on the instance, so a
    successful fetch proves nothing on its own — the project's namespace chain
    has to actually contain the candidate org.
    """
    try:
        project_info = await gitlab_plugin.fetch_project(
            candidate.access_token, project_id, provider_url=candidate.provider_url
        )
    except Exception:
        return None

    namespace_id = str((project_info.get("namespace") or {}).get("id", ""))
    if not namespace_id:
        return None

    chain = {namespace_id}
    if candidate.external_org_id != namespace_id:
        try:
            ancestors = await gitlab_plugin.fetch_group_ancestors(
                candidate.access_token, namespace_id, provider_url=candidate.provider_url
            )
        except Exception:
            return None
        chain.update(str(ancestor["id"]) for ancestor in ancestors)

    return project_info if candidate.external_org_id in chain else None


async def ensure_project(
    payload: dict[str, Any],
    instance_url: str | None,
    db_plugin: Any,
    gitlab_plugin: Any,
) -> WebhookResponse:
    """Write (or refresh) the repo row for a project a connected group owns."""
    project_id = str(payload.get("project_id", ""))
    project_path = payload.get("path_with_namespace", project_id)
    namespace_id = str(payload.get("project_namespace_id", ""))

    with db_plugin.session() as db:
        candidates = _collect_candidates(db, db_plugin, namespace_id, instance_url)

    if not candidates:
        return WebhookResponse(message="No connected GitLab group to match", processed=True)

    for candidate in candidates:
        project_info = await _claim_project(gitlab_plugin, candidate, project_id)
        if project_info:
            return await _sync_claimed_project(
                db_plugin, gitlab_plugin, candidate, project_info, project_id, project_path
            )

    return WebhookResponse(
        message=f"Project {project_path} is outside every connected group",
        processed=True,
    )


def remove_project(payload: dict[str, Any], db_plugin: Any) -> WebhookResponse:
    """Drop the repo row for a project that no longer exists."""
    project_id = str(payload.get("project_id", ""))
    project_path = payload.get("path_with_namespace", project_id)

    with db_plugin.session() as db:
        repo = db_get_repository_by_external_id(db, project_id)
        if not repo or repo.provider != "gitlab":
            return WebhookResponse(message=f"Project {project_path} not tracked", processed=True)
        db_delete_repository(db, project_id)

    return WebhookResponse(message=f"Removed project {project_path}", processed=True)


async def _sync_claimed_project(
    db_plugin: Any,
    gitlab_plugin: Any,
    candidate: Candidate,
    project_info: dict[str, Any],
    project_id: str,
    project_path: str,
) -> WebhookResponse:
    """Write the repo row under its namespace org and queue the MR/issue backfill."""
    repo_name = project_info.get("path_with_namespace") or project_info.get("name", project_path)
    avatar_url = project_info.get("avatar_url") or (project_info.get("namespace") or {}).get(
        "avatar_url"
    )

    # Resolved first: it can cost a GitLab round trip, which the write session
    # below must not be open across.
    try:
        target_org_id = await _resolve_project_org(
            db_plugin,
            gitlab_plugin,
            candidate.access_token,
            candidate.provider_url,
            candidate.base_url,
            candidate.workspace_id,
            candidate.org_id,
            candidate.external_org_id,
            {},
            project_info,
        )
    except Exception as e:
        logger.warning(
            f"Failed to resolve namespace org for project {repo_name}: {e}", exc_info=True
        )
        return WebhookResponse(message=f"Could not resolve org for {repo_name}", processed=False)

    def _write_repo(db: Session) -> tuple[bool, str | None]:
        # A transfer moves the project into another namespace, so an existing
        # row has to follow it rather than gain a duplicate under the new org.
        existing = db_get_repository_by_external_id(db, project_id)
        is_new = existing is None
        if existing and existing.org_id != target_org_id:
            existing.org_id = target_org_id
            db.commit()

        db_upsert_repository(
            db=db,
            org_id=target_org_id,
            external_id=project_id,
            name=repo_name,
            web_url=project_info.get("web_url"),
            provider="gitlab",
            provider_url=candidate.provider_url,
            avatar_url=avatar_url,
        )

        org = db_get_org_by_id(db, target_org_id)
        return is_new, str(org.workspace_id) if org and org.workspace_id else None

    is_new, workspace_id = await db_plugin.run_in_session(_write_repo)

    if is_new:
        # New to us — its open MRs and issues predate any webhook we'd receive.
        try:
            broker = get_faststream_broker()
            await broker.publish(
                GitLabSyncProjectRepositoryMessage(
                    org_id=str(target_org_id), project_id=project_id
                ),
                stream="jeanclode.events.gitlab.sync_project_repository",
                maxlen=STREAM_MAXLEN,
            )
        except Exception as e:
            logger.error(f"Failed to queue backfill for project {project_id}: {e}", exc_info=True)

    if workspace_id:
        await publish_sync_event(
            workspace_id=workspace_id,
            action="repos_synced",
            payload={"org_id": str(target_org_id), "repo_count": 1},
        )

    logger.info(f"Synced GitLab project {repo_name} ({project_id})")
    return WebhookResponse(message=f"Synced project {repo_name}", processed=True)
