"""FastAPI dependencies for GitLab webhook verification."""

import hmac

from fastapi import Header, HTTPException, Request

from api.context import get_current_app


async def verify_gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(None, description="GitLab webhook secret token"),
) -> bytes:
    """Verify GitLab webhook: plugin enabled + token match.

    GitLab sends the secret token in the X-Gitlab-Token header (plain comparison).

    Returns:
        Raw request body for downstream handlers.

    Raises:
        HTTPException: 404 if GitLab plugin not configured, 401 if token invalid.
    """
    app = get_current_app()
    gitlab_plugin = app.gitlab

    if not gitlab_plugin:
        raise HTTPException(status_code=404, detail="GitLab integration not configured")

    webhook_secret = gitlab_plugin.get_effective_webhook_secret()
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if not x_gitlab_token:
        raise HTTPException(status_code=401, detail="Missing X-Gitlab-Token header")

    if not hmac.compare_digest(webhook_secret, x_gitlab_token):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    return await request.body()
