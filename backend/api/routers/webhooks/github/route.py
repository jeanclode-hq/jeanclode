"""GitHub webhook handlers — unified router."""

import json
import logging

from fastapi import APIRouter, Depends, Header, Request

from api.database import run_in_session
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker

from ..utils import WebhookResponse
from .dependency import verify_github_webhook
from .installations import (
    handle_installation_created,
    handle_installation_deleted,
    handle_repositories_added,
    handle_repositories_removed,
    handle_repository_event,
)
from .memberships import handle_member_added, handle_member_removed
from .schemas import (
    GitHubAppInstallationEvent,
    GitHubAppInstallationRepositoriesEvent,
    GitHubOrganizationMembershipEvent,
    GitHubRepositoryEvent,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/github", tags=["Webhooks", "GitHub"], dependencies=[Depends(verify_github_webhook)]
)


@router.post(
    "",
    operation_id="github_webhook",
    response_model=WebhookResponse,
    status_code=202,
)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., description="GitHub event type"),
) -> WebhookResponse:
    """Unified GitHub webhook router — routes by X-GitHub-Event header."""
    body = await request.body()

    match x_github_event:
        case "installation":
            event_data = json.loads(body)
            installation_event = GitHubAppInstallationEvent(**event_data)

            match installation_event.action:
                case "created":
                    return await handle_installation_created(installation_event)
                case "deleted":
                    return await run_in_session(
                        lambda db: handle_installation_deleted(installation_event, db)
                    )
                case _:
                    return WebhookResponse(
                        message=f"Installation action '{installation_event.action}' not processed",
                        processed=False,
                    )

        case "installation_repositories":
            event_data = json.loads(body)
            repo_event = GitHubAppInstallationRepositoriesEvent(**event_data)

            match repo_event.action:
                case "added":
                    return await handle_repositories_added(repo_event)
                case "removed":
                    return await run_in_session(
                        lambda db: handle_repositories_removed(repo_event, db)
                    )
                case _:
                    return WebhookResponse(
                        message=f"Repositories action '{repo_event.action}' not processed",
                        processed=False,
                    )

        case "repository":
            event_data = json.loads(body)
            return await handle_repository_event(GitHubRepositoryEvent(**event_data))

        case "pull_request":
            broker = get_faststream_broker()
            await broker.publish(
                body,
                stream="jeanclode.events.github.pull_requests",
                maxlen=STREAM_MAXLEN,
            )
            return WebhookResponse(message="pull_request event queued", processed=True)

        case "issues":
            broker = get_faststream_broker()
            await broker.publish(
                body,
                stream="jeanclode.events.github.issues",
                maxlen=STREAM_MAXLEN,
            )
            return WebhookResponse(message="issues event queued", processed=True)

        case "organization":
            event_data = json.loads(body)
            org_event = GitHubOrganizationMembershipEvent(**event_data)

            match org_event.action:
                case "member_added":
                    return await run_in_session(lambda db: handle_member_added(org_event, db))
                case "member_removed":
                    return await run_in_session(lambda db: handle_member_removed(org_event, db))
                case _:
                    return WebhookResponse(
                        message=f"Organization action '{org_event.action}' not processed",
                        processed=False,
                    )

        case "issue_comment" | "pull_request_review" | "pull_request_review_comment":
            # All three carry potential @jeanclode mentions. A single
            # downstream consumer normalizes the event type and decides
            # whether to dispatch — keeps wiring lean.
            broker = get_faststream_broker()
            await broker.publish(
                {"event_type": x_github_event, "payload": json.loads(body)},
                stream="jeanclode.events.github.mentions",
                maxlen=STREAM_MAXLEN,
            )
            return WebhookResponse(message=f"{x_github_event} event queued", processed=True)

        case _:
            return WebhookResponse(
                message=f"Event type '{x_github_event}' not supported",
                processed=False,
            )
