"""Auth route handlers — OAuth login, sessions, profile, provider linking."""

import logging
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database import get_session
from api.database.identity import (
    db_count_user_identities,
    db_get_identity_by_external_id,
    db_get_or_create_provider_identity,
    db_unlink_identity_from_user,
)
from api.database.user import db_get_user_by_email
from api.database.workspace import db_get_workspaces_by_user, db_is_workspace_member
from api.models import User

from .dependencies import get_current_user, require_provider_enabled
from .enums import OAuthProvider
from .schemas import (
    DisconnectProviderResponse,
    LogoutResponse,
    UpdateProfileRequest,
    UpdateProfileResponse,
    UserProfile,
)
from .utils import get_oauth_client, handle_oauth_user_creation, set_session_cookie

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


# =============================================================================
# OAuth Login Endpoints
# =============================================================================


@router.get(
    "/github/authorize",
    operation_id="login_github",
    name="github_login",
    response_class=RedirectResponse,
    status_code=302,
)
async def github_login(request: Request) -> RedirectResponse:
    """Initiate GitHub OAuth login with PKCE."""
    await require_provider_enabled(OAuthProvider.GITHUB)
    redirect_uri = request.url_for("oauth_callback", provider=OAuthProvider.GITHUB.value)
    client = get_oauth_client(OAuthProvider.GITHUB)
    return await client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]


@router.get(
    "/gitlab/authorize",
    operation_id="login_gitlab",
    name="gitlab_login",
    response_class=RedirectResponse,
    status_code=302,
)
async def gitlab_login(request: Request) -> RedirectResponse:
    """Initiate GitLab OAuth login with PKCE."""
    await require_provider_enabled(OAuthProvider.GITLAB)
    redirect_uri = request.url_for("oauth_callback", provider=OAuthProvider.GITLAB.value)
    client = get_oauth_client(OAuthProvider.GITLAB)
    return await client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]


# =============================================================================
# OAuth Callback
# =============================================================================


async def _exchange_oauth_token(provider: OAuthProvider, request: Request) -> dict:
    """Exchange OAuth authorization code for access token."""
    app = get_current_app()
    oauth_client = get_oauth_client(provider)
    token: dict = await oauth_client.authorize_access_token(request)

    if provider == OAuthProvider.GITLAB:
        gitlab_plugin = app.gitlab
        if gitlab_plugin:
            access_token = token.get("access_token")
            if access_token:
                try:
                    instance_url = gitlab_plugin.get_effective_instance_url()
                    userinfo = await gitlab_plugin.verify_token(
                        access_token, provider_url=instance_url
                    )
                    token["userinfo"] = userinfo
                except Exception as e:
                    logger.error(f"Failed to fetch GitLab userinfo: {e}", exc_info=True)
                    raise HTTPException(
                        status_code=500, detail="Failed to fetch GitLab user info"
                    ) from e

    return token


async def _handle_link_callback(
    provider: OAuthProvider,
    link_user_id: str,
    token: dict,
    frontend_url: str,
) -> RedirectResponse:
    """Handle link-mode OAuth callback: link a new provider identity to existing user."""
    logger.info(f"Link mode: linking {provider.value} to user {link_user_id}")

    app = get_current_app()
    if not app.database:
        raise HTTPException(status_code=503, detail="Database not available")

    oauth_client = get_oauth_client(provider)
    userinfo = token.get("userinfo", {})

    if provider == OAuthProvider.GITHUB and not userinfo:
        response = await oauth_client.get("user", token=token)
        userinfo = response.json()

    external_id = str(userinfo.get("id") or userinfo.get("sub") or "")
    username = userinfo.get("login") or userinfo.get("username")
    avatar_url = userinfo.get("avatar_url") or userinfo.get("picture")

    if not external_id:
        return RedirectResponse(
            url=f"{frontend_url}/settings?error=no_external_id", status_code=302
        )

    with app.database.session() as db:
        existing_identity = db_get_identity_by_external_id(db, provider.value, external_id)
        if (
            existing_identity
            and existing_identity.user_id
            and str(existing_identity.user_id) != link_user_id
        ):
            return RedirectResponse(
                url=f"{frontend_url}/settings?error=account_in_use", status_code=302
            )

        db_get_or_create_provider_identity(
            db=db,
            provider=provider.value,
            external_id=external_id,
            username=username,
            avatar_url=avatar_url,
            user_id=UUID(link_user_id),
        )

    return RedirectResponse(url=f"{frontend_url}/settings?linked={provider.value}", status_code=302)


async def _handle_login_callback(provider: OAuthProvider, token: dict) -> RedirectResponse:
    """Handle login-mode OAuth callback: create/update user, create Redis session, set cookie."""
    app = get_current_app()
    if not app.web:
        raise RuntimeError("Web plugin not enabled")

    frontend_url = app.web.config.frontend_url

    user = await handle_oauth_user_creation(provider, token)

    # Auto-join: if the user already has workspace memberships (pre-populated by org sync),
    # ensure last_workspace_id points to one so the frontend skips the create-workspace flow.
    if app.database:
        with app.database.session() as db:
            workspaces = db_get_workspaces_by_user(db, user.id)
            if workspaces:
                workspace_ids = {ws.id for ws in workspaces}
                if user.last_workspace_id not in workspace_ids:
                    db_user = db.merge(user)
                    db_user.last_workspace_id = workspaces[0].id
                    db.commit()

    # Create Redis session
    session_id = await app.web.sessions.create(user.id)

    response = RedirectResponse(url=f"{frontend_url}/", status_code=302)
    set_session_cookie(
        response,
        session_id,
        app.web.config.session,
    )
    return response


@router.get(
    "/callback/{provider}",
    operation_id="oauth_callback",
    name="oauth_callback",
    response_class=RedirectResponse,
    status_code=302,
)
async def oauth_callback(provider: OAuthProvider, request: Request) -> RedirectResponse:
    """Handle OAuth callback from any provider."""
    await require_provider_enabled(provider)

    app = get_current_app()
    if not app.web:
        raise RuntimeError("Web plugin not enabled")

    frontend_url = app.web.config.frontend_url
    token = await _exchange_oauth_token(provider, request)

    # Check for link mode (provider linking flow)
    link_mode = request.session.pop("link_mode", None)
    link_user_id = request.session.pop("link_user_id", None)
    if link_mode and link_user_id:
        return await _handle_link_callback(provider, link_user_id, token, frontend_url)

    return await _handle_login_callback(provider, token)


# =============================================================================
# User Profile Endpoints
# =============================================================================


@router.get(
    "/me",
    operation_id="get_me",
    name="get_current_user_profile",
    response_model=UserProfile,
)
async def get_me(current_user: User = Depends(get_current_user)) -> UserProfile:
    """Get current authenticated user profile."""
    return UserProfile.from_user(current_user)


@router.patch(
    "/me",
    operation_id="update_profile",
    name="update_profile",
    response_model=UpdateProfileResponse,
)
def update_profile(
    request_body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UpdateProfileResponse:
    """Update current user's profile."""
    current_user = db.merge(current_user)

    if request_body.email is not None:
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if request_body.email and not re.match(email_pattern, request_body.email):
            raise HTTPException(status_code=400, detail="Invalid email format")

        if request_body.email:
            existing = db_get_user_by_email(db, request_body.email)
            if existing and existing.id != current_user.id:
                raise HTTPException(status_code=400, detail="Email already in use")

        current_user.email = request_body.email if request_body.email else None

    if request_body.onboarding_step is not None:
        current_user.onboarding_step = request_body.onboarding_step

    if request_body.last_workspace_id is not None:
        if not db_is_workspace_member(db, request_body.last_workspace_id, current_user.id):
            raise HTTPException(status_code=403, detail="You don't have access to this workspace")
        current_user.last_workspace_id = request_body.last_workspace_id

    db.commit()
    db.refresh(current_user)

    return UpdateProfileResponse(profile=UserProfile.from_user(current_user))


# =============================================================================
# Logout
# =============================================================================


@router.post(
    "/logout",
    operation_id="logout",
    name="logout",
    response_model=LogoutResponse,
)
async def logout(request: Request) -> Response:
    """Logout — delete Redis session and clear cookie."""
    app = get_current_app()
    if not app.web:
        raise RuntimeError("Web plugin not enabled")

    jeanclode_session = request.cookies.get("jeanclode_session")
    if jeanclode_session:
        await app.web.sessions.delete(jeanclode_session)

    logout_data = LogoutResponse(message="Logged out successfully")
    response = Response(
        content=logout_data.model_dump_json(),
        media_type="application/json",
        status_code=200,
    )
    set_session_cookie(
        response,
        "",
        app.web.config.session,
        max_age_seconds=0,
    )
    return response


# =============================================================================
# Provider Linking / Unlinking
# =============================================================================


@router.get(
    "/link/{provider}",
    operation_id="link_provider",
    name="link_provider",
    response_class=RedirectResponse,
    status_code=302,
)
async def link_provider(
    provider: OAuthProvider,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Initiate OAuth flow to link an additional provider to the current user."""
    await require_provider_enabled(provider)

    app = get_current_app()
    if not app.web:
        raise RuntimeError("Web plugin not enabled")

    frontend_url = app.web.config.frontend_url

    identity = current_user.get_identity(provider.value)
    if identity:
        return RedirectResponse(
            url=f"{frontend_url}/settings?error=already_linked", status_code=302
        )

    # Store link state in Starlette session (OAuth state cookie)
    request.session["link_mode"] = provider.value
    request.session["link_user_id"] = str(current_user.id)

    redirect_uri = request.url_for("oauth_callback", provider=provider.value)
    oauth_client = get_oauth_client(provider)
    return await oauth_client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]


@router.delete(
    "/providers/{provider}",
    operation_id="disconnect_provider",
    name="disconnect_provider",
    response_model=DisconnectProviderResponse,
)
def disconnect_provider(
    provider: OAuthProvider,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> DisconnectProviderResponse:
    """Disconnect a provider from the user's account (min 1 remaining)."""
    identity_count = db_count_user_identities(db, current_user.id)

    if identity_count <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot disconnect: You must have at least one connected account",
        )

    identity = current_user.get_identity(provider.value)
    if not identity:
        raise HTTPException(
            status_code=404,
            detail=f"Provider {provider.value} is not connected to your account",
        )

    success = db_unlink_identity_from_user(db, current_user.id, provider.value)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to disconnect provider")

    current_user = db.merge(current_user)
    db.refresh(current_user)

    return DisconnectProviderResponse(
        message=f"{provider.value.capitalize()} account disconnected",
        profile=UserProfile.from_user(current_user),
    )
