"""Admin route handlers — settings CRUD + GitHub manifest flow gated by ADMIN_SECRET."""

import hmac
import logging
import secrets
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import get_session, run_in_session
from api.services.instance_settings import (
    delete_category,
    load_github_config,
    load_gitlab_config,
    save_github_config,
    save_gitlab_config,
)
from api.services.llm_credentials import (
    create_llm_credential,
    delete_llm_credential,
    list_llm_credentials,
    reorder_llm_credentials,
    update_llm_credential,
)

from .dependencies import ADMIN_SESSION_PREFIX, require_admin, require_admin_enabled
from .schemas import (
    AdminAuthRequest,
    AdminAuthResponse,
    AdminSettingsResponse,
    GitHubConfigInput,
    GitHubConfigView,
    GithubManifestResponse,
    GitLabConfigInput,
    GitLabConfigView,
    LLMCredentialInput,
    LLMCredentialReorderInput,
    LLMCredentialUpdateInput,
    LLMCredentialView,
    MessageResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


RATE_LIMIT_PREFIX = "admin:ratelimit:"
MANIFEST_STATE_PREFIX = "github:manifest:state:"
MANIFEST_STATE_TTL = 600
MASK = "****"


def _get_redis():  # type: ignore[no-untyped-def]
    app = get_current_app()
    if not app.faststream:
        raise HTTPException(status_code=503, detail="Redis not available")
    return app.faststream.get_redis()


def _get_admin_config():  # type: ignore[no-untyped-def]
    app = get_current_app()
    if not app.web:
        raise HTTPException(status_code=503, detail="Web plugin not available")
    return app.web.config.admin


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only if explicitly configured to be behind a proxy.
    app = get_current_app()
    behind_proxy = bool(app.web and app.web.config.behind_proxy)
    if behind_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_rate_limit(request: Request) -> None:
    """Raise 429 if the client IP has exceeded the admin auth rate limit."""
    admin_cfg = _get_admin_config()
    redis = _get_redis()
    ip = _client_ip(request)
    key = f"{RATE_LIMIT_PREFIX}{ip}"

    current_raw = await redis.get(key)
    current = int(current_raw) if current_raw else 0
    if current >= admin_cfg.rate_limit_max_attempts:
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Try again later.",
        )


async def _record_failed_attempt(request: Request) -> None:
    admin_cfg = _get_admin_config()
    redis = _get_redis()
    ip = _client_ip(request)
    key = f"{RATE_LIMIT_PREFIX}{ip}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, admin_cfg.rate_limit_window_seconds)


async def _clear_failed_attempts(request: Request) -> None:
    redis = _get_redis()
    ip = _client_ip(request)
    await redis.delete(f"{RATE_LIMIT_PREFIX}{ip}")


def _set_admin_cookie(response: Response, session_id: str, *, clear: bool = False) -> None:
    app = get_current_app()
    admin_cfg = _get_admin_config()
    is_production = bool(app._backend_config and app._backend_config.is_production)
    response.set_cookie(
        key=admin_cfg.cookie_name,
        value="" if clear else session_id,
        path="/",
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=0 if clear else admin_cfg.session_ttl_seconds,
    )


def _frontend_url() -> str:
    app = get_current_app()
    if not app.web:
        raise HTTPException(status_code=503, detail="Web plugin not available")
    return app.web.config.frontend_url


def _backend_base_url(request: Request) -> str:
    """Derive this backend's public base URL from the incoming request.

    Used only as a fallback when the admin doesn't provide ``public_url`` in
    the manifest form. In dev this returns ``http://localhost:8000`` which
    fails ``_is_public_url`` and forces the admin to supply a tunnel URL.
    """
    return str(request.base_url).rstrip("/")


def _is_public_url(url: str) -> bool:
    """Return True if ``url`` is plausibly reachable from the public Internet.

    GitHub refuses manifests whose ``hook_attributes.url`` points at
    localhost / 127.0.0.1 / private IPs, so we omit the hook entirely in
    those cases.
    """
    lowered = url.lower()
    return not any(
        marker in lowered
        for marker in ("localhost", "127.0.0.1", "0.0.0.0", "://host.docker.internal")
    )


def _mask_dict(config: dict | None, secret_keys: set[str]) -> dict | None:
    if not config:
        return None
    masked = {}
    for key, value in config.items():
        masked[key] = MASK if key in secret_keys and value else value
    return masked


# =============================================================================
# Auth
# =============================================================================


@router.post("/auth", operation_id="admin_auth", response_model=AdminAuthResponse)
async def admin_auth(
    body: AdminAuthRequest,
    request: Request,
    response: Response,
) -> AdminAuthResponse:
    """Exchange the admin secret for a Redis-backed session cookie."""
    require_admin_enabled()
    admin_cfg = _get_admin_config()
    await _check_rate_limit(request)

    expected = admin_cfg.secret.encode("utf-8")
    provided = body.secret.encode("utf-8")
    if not hmac.compare_digest(expected, provided):
        await _record_failed_attempt(request)
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    await _clear_failed_attempts(request)

    redis = _get_redis()
    session_id = secrets.token_urlsafe(32)
    await redis.set(
        f"{ADMIN_SESSION_PREFIX}{session_id}",
        "1",
        ex=admin_cfg.session_ttl_seconds,
    )
    _set_admin_cookie(response, session_id)
    return AdminAuthResponse()


@router.post("/logout", operation_id="admin_logout", response_model=MessageResponse)
async def admin_logout(request: Request, response: Response) -> MessageResponse:
    """Clear the admin session cookie and delete the Redis session."""
    admin_cfg = _get_admin_config()
    cookie_value = request.cookies.get(admin_cfg.cookie_name)
    if cookie_value:
        redis = _get_redis()
        await redis.delete(f"{ADMIN_SESSION_PREFIX}{cookie_value}")
    _set_admin_cookie(response, "", clear=True)
    return MessageResponse(message="Logged out")


# =============================================================================
# Settings (read / update / delete)
# =============================================================================


@router.get(
    "/settings",
    operation_id="admin_get_settings",
    response_model=AdminSettingsResponse,
    dependencies=[Depends(require_admin)],
)
def get_admin_settings(db: Session = Depends(get_session)) -> AdminSettingsResponse:
    """Return all instance settings with secrets masked."""
    github = load_github_config(db)
    gitlab = load_gitlab_config(db)

    github_view = (
        GitHubConfigView(
            **(_mask_dict(github, {"client_secret", "private_key_pem", "webhook_secret"}) or {})
        )
        if github
        else None
    )
    gitlab_view = (
        GitLabConfigView(**(_mask_dict(gitlab, {"client_secret", "webhook_secret"}) or {}))
        if gitlab
        else None
    )

    return AdminSettingsResponse(github=github_view, gitlab=gitlab_view)


@router.put(
    "/settings/github",
    operation_id="admin_put_github",
    response_model=MessageResponse,
    dependencies=[Depends(require_admin)],
)
def put_github_settings(
    body: GitHubConfigInput, db: Session = Depends(get_session)
) -> MessageResponse:
    save_github_config(db, body.model_dump())
    return MessageResponse(message="GitHub config saved")


@router.put(
    "/settings/gitlab",
    operation_id="admin_put_gitlab",
    response_model=MessageResponse,
    dependencies=[Depends(require_admin)],
)
def put_gitlab_settings(
    body: GitLabConfigInput, db: Session = Depends(get_session)
) -> MessageResponse:
    save_gitlab_config(db, body.model_dump())
    return MessageResponse(message="GitLab config saved")


@router.delete(
    "/settings/{category}",
    operation_id="admin_delete_category",
    response_model=MessageResponse,
    dependencies=[Depends(require_admin)],
)
def delete_settings_category(
    category: Literal["github", "gitlab"],
    db: Session = Depends(get_session),
) -> MessageResponse:
    count = delete_category(db, category)
    # Both GitHub and GitLab plugins are stateless — they read DB per request,
    # so admin writes take effect immediately across all pods with no reload.
    return MessageResponse(message=f"Deleted {count} {category} settings")


# =============================================================================
# LLM credential pool (ADR-010)
# =============================================================================


def _llm_credential_view(credential) -> LLMCredentialView:  # type: ignore[no-untyped-def]
    return LLMCredentialView(
        id=str(credential.id),
        priority=credential.priority,
        kind=credential.kind,
        provider=credential.provider,
        plan_tier=credential.plan_tier,
        secret=MASK,
        name=credential.name,
        model_high=credential.model_high,
        model_heavy=credential.model_heavy,
        model_low=credential.model_low,
        base_url=credential.base_url,
        status=credential.status,
        stale_until=credential.stale_until.isoformat() if credential.stale_until else None,
    )


@router.get(
    "/llm-credentials",
    operation_id="admin_list_llm_credentials",
    response_model=list[LLMCredentialView],
    dependencies=[Depends(require_admin)],
)
def get_llm_credentials(db: Session = Depends(get_session)) -> list[LLMCredentialView]:
    """List the LLM credential pool, ordered by priority, secrets masked."""
    return [_llm_credential_view(c) for c in list_llm_credentials(db)]


@router.post(
    "/llm-credentials",
    operation_id="admin_create_llm_credential",
    response_model=LLMCredentialView,
    dependencies=[Depends(require_admin)],
)
def post_llm_credential(
    body: LLMCredentialInput, db: Session = Depends(get_session)
) -> LLMCredentialView:
    """Append a new credential to the end of the priority order."""
    credential = create_llm_credential(
        db,
        kind=body.kind,
        provider=body.provider,
        secret=body.secret,
        name=body.name,
        model_high=body.model_high,
        model_heavy=body.model_heavy,
        model_low=body.model_low,
        base_url=body.base_url,
        plan_tier=body.plan_tier,
    )
    return _llm_credential_view(credential)


@router.put(
    "/llm-credentials/{credential_id}",
    operation_id="admin_update_llm_credential",
    response_model=LLMCredentialView,
    dependencies=[Depends(require_admin)],
)
def put_llm_credential(
    credential_id: UUID,
    body: LLMCredentialUpdateInput,
    db: Session = Depends(get_session),
) -> LLMCredentialView:
    """Update a credential. An empty ``secret`` leaves the stored value unchanged."""
    credential = update_llm_credential(
        db,
        credential_id,
        kind=body.kind,
        provider=body.provider,
        secret=body.secret,
        name=body.name,
        model_high=body.model_high,
        model_heavy=body.model_heavy,
        model_low=body.model_low,
        base_url=body.base_url,
        plan_tier=body.plan_tier,
    )
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    return _llm_credential_view(credential)


@router.delete(
    "/llm-credentials/{credential_id}",
    operation_id="admin_delete_llm_credential",
    response_model=MessageResponse,
    dependencies=[Depends(require_admin)],
)
def delete_llm_credential_route(
    credential_id: UUID, db: Session = Depends(get_session)
) -> MessageResponse:
    deleted = delete_llm_credential(db, credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")
    return MessageResponse(message="Credential deleted")


@router.post(
    "/llm-credentials/reorder",
    operation_id="admin_reorder_llm_credentials",
    response_model=list[LLMCredentialView],
    dependencies=[Depends(require_admin)],
)
def post_reorder_llm_credentials(
    body: LLMCredentialReorderInput, db: Session = Depends(get_session)
) -> list[LLMCredentialView]:
    """Reassign priority 1..N following the given order.

    ``ordered_ids`` must contain exactly the existing credential ids.
    """
    try:
        ordered_ids = [UUID(cid) for cid in body.ordered_ids]
        credentials = reorder_llm_credentials(db, ordered_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return [_llm_credential_view(c) for c in credentials]


# =============================================================================
# GitHub App manifest flow
# =============================================================================


@router.get(
    "/github/manifest",
    operation_id="admin_github_manifest",
    response_model=GithubManifestResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_github_manifest(
    request: Request,
    public_url: str | None = None,
    name: str = "jeanclode-bot",
    account_type: Literal["personal", "org"] = "personal",
    org_slug: str | None = None,
) -> GithubManifestResponse:
    """Generate a GitHub App manifest and CSRF state for the manifest flow.

    ``installation`` is not a subscribable event — GitHub delivers installation
    lifecycle payloads to every App automatically, and including it in
    ``default_events`` makes the manifest invalid.

    GitHub requires ``hook_attributes.url`` to be a publicly reachable URL.
    In dev the backend is usually on localhost, so the caller must pass
    ``public_url`` pointing at a tunnel (ngrok, cloudflared, etc). That URL
    is also used for ``redirect_url`` so the manifest conversion callback
    actually hits *this* backend.

    ``account_type`` selects which GitHub URL the user is redirected to:
    personal settings or an organization's settings (requires ``org_slug``).

    Homepage, setup, OAuth-callback, and webhook URLs are all derived from
    the backend's ``frontend_url`` config and known backend routes — not
    admin-overridable. A misconfigured URL would silently break OAuth/webhook
    delivery, so we keep them authoritative.
    """
    if account_type == "org" and not org_slug:
        raise HTTPException(
            status_code=400,
            detail="org_slug is required when account_type is 'org'.",
        )

    state = secrets.token_urlsafe(32)
    redis = _get_redis()
    await redis.set(f"{MANIFEST_STATE_PREFIX}{state}", "1", ex=MANIFEST_STATE_TTL)

    # Two different concerns share the "backend URL" name, and they must be
    # kept separate in dev:
    #   - ``webhook_base``: where GitHub's servers call us (webhooks, manifest
    #     redirect). Must be publicly reachable from the Internet, hence the
    #     admin-provided tunnel URL in dev.
    #   - ``oauth_base``: where the user's browser calls us to do OAuth login.
    #     This is whatever URL the frontend's apiBase points at — typically
    #     localhost in dev, the deployed backend URL in prod.
    # In prod the two are the same public URL. In dev (tunnel for webhooks,
    # localhost for browser), they diverge — and the callback_urls registered
    # on the App must match what the browser actually sends as redirect_uri.
    webhook_base = (public_url or _backend_base_url(request)).rstrip("/")
    if not _is_public_url(webhook_base):
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub requires a public webhook URL. Provide a tunnel URL "
                "(e.g. https://<subdomain>.ngrok-free.app) in the 'Public "
                "backend URL' field before creating the App."
            ),
        )
    oauth_base = _backend_base_url(request).rstrip("/")

    frontend = _frontend_url().rstrip("/")
    manifest: dict = {
        "name": name,
        "url": frontend,
        # Browser redirect after manifest confirmation — browser reaches us
        # directly, so use the oauth_base (localhost in dev).
        "redirect_url": f"{oauth_base}/admin/github/manifest/callback",
        # Browser-initiated OAuth callback — must match what authlib builds
        # from request.base_url at login time.
        "callback_urls": [f"{oauth_base}/auth/callback/github"],
        # Where GitHub redirects users after they install the App. Points at
        # the frontend root so the app's own routing/middleware decides where
        # to land (dashboard if logged in, login page otherwise). /admin
        # would be wrong for anyone who isn't the instance admin.
        "setup_url": f"{frontend}/",
        # GitHub-initiated webhook delivery — must be public.
        "hook_attributes": {"url": f"{webhook_base}/webhooks/github"},
        # Pairing install with OAuth triggers a callback that authlib rejects
        # (no matching state in session — the flow wasn't user-initiated).
        # Install is install; user login is a separate "Sign in with GitHub"
        # action from the frontend.
        "request_oauth_on_install": False,
        "setup_on_update": True,
        "public": False,
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "workflows": "write",
            "metadata": "read",
            "members": "read",
            # ci-watch (ADR-008): reads CI results on the fixer's pushed
            # commit before marking a PR ready for review.
            "checks": "read",
            "statuses": "read",
            "actions": "read",
            "administration": "read",
            # User-level permission — required so the OAuth token can call /user/emails
            # and fetch secondary verified addresses for cross-provider account merging.
            "emails": "read",
        },
        "default_events": [
            "push",
            "pull_request",
            "issues",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
            # An installation scoped to all repositories does not reliably
            # announce a newly created repo through installation_repositories;
            # this is what keeps the repo list current (and catches renames).
            "repository",
        ],
        "description": "Autonomous Sentry error triage and fix bot.",
    }

    if account_type == "org":
        github_url = f"https://github.com/organizations/{org_slug}/settings/apps/new"
    else:
        github_url = "https://github.com/settings/apps/new"

    return GithubManifestResponse(manifest=manifest, state=state, github_url=github_url)


@router.get(
    "/github/manifest/callback",
    operation_id="admin_github_manifest_callback",
)
async def admin_github_manifest_callback(code: str, state: str) -> RedirectResponse:
    """Exchange the manifest ``code`` for GitHub App credentials.

    GitHub redirects the user's browser here after they confirm the manifest.
    Intentionally NOT gated by ``require_admin`` — when the admin typed a
    public tunnel URL (ngrok etc.) as ``public_url``, that host is a
    different origin than where the admin session cookie was set, so the
    cookie isn't sent on this top-level redirect. Security is instead
    enforced by the one-shot CSRF ``state`` token: it was generated inside
    the admin-gated ``/admin/github/manifest`` endpoint, stored in Redis
    with a 10-minute TTL, and is DEL'd here on first use. An attacker
    without access to the admin session cannot produce a valid state.
    """
    redis = _get_redis()
    state_key = f"{MANIFEST_STATE_PREFIX}{state}"
    # GETDEL is atomic — eliminates the race where two concurrent callbacks
    # both pass the GET check before either deletes the key.
    stored = await redis.getdel(state_key)
    if not stored:
        raise HTTPException(status_code=400, detail="Invalid or expired manifest state")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://api.github.com/app-manifests/{code}/conversions",
            headers={"Accept": "application/vnd.github+json"},
        )
    if resp.status_code not in (200, 201):
        logger.error(f"GitHub manifest conversion failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="GitHub manifest conversion failed")

    data = resp.json()
    try:
        owner = data.get("owner") or {}
        config = {
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "app_id": str(data["id"]),
            "private_key_pem": data["pem"],
            "webhook_secret": data.get("webhook_secret", ""),
            "name": data.get("name", "jeanclode"),
            "owner_login": owner.get("login"),
            "owner_type": owner.get("type"),
        }
    except KeyError as e:  # pragma: no cover - defensive
        logger.error(f"GitHub manifest response missing field: {e}")
        raise HTTPException(status_code=502, detail="Invalid GitHub manifest response") from e

    await run_in_session(lambda db: save_github_config(db, config))

    return RedirectResponse(url=f"{_frontend_url()}/admin", status_code=302)
