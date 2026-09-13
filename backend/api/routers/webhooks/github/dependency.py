"""FastAPI dependencies for GitHub webhook verification."""

import hashlib
import hmac

from fastapi import Header, HTTPException, Request

from api.context import get_current_app


async def verify_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, description="GitHub webhook signature"),
) -> None:
    """Verify GitHub webhook: plugin enabled + HMAC signature.

    Raises:
        HTTPException: 404 if GitHub plugin not configured, 401 if signature invalid
    """
    app = get_current_app()
    github_plugin = app.github

    if not github_plugin:
        raise HTTPException(status_code=404, detail="GitHub integration not configured")

    effective_app = github_plugin.get_effective_app()
    if not effective_app:
        raise HTTPException(status_code=404, detail="GitHub integration not configured")

    webhook_secret = effective_app.webhook_secret
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    body = await request.body()
    computed = "sha256=" + hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
