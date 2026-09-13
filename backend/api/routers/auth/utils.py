"""Auth utilities — OAuth client access, cookie helpers, user creation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from fastapi import Response

from api.context import get_current_app
from api.database.user import db_create_or_update_user
from api.models.users import User
from api.plugins.faststream import STREAM_MAXLEN, get_faststream_broker

from .enums import OAuthProvider
from .schemas import SyncUserMembershipsMessage

if TYPE_CHECKING:
    from api.plugins.web.config import SessionConfig

logger = logging.getLogger(__name__)


# =============================================================================
# OAuth client
# =============================================================================


def get_oauth_client(provider: OAuthProvider):  # type: ignore[no-untyped-def]
    """Get the authlib OAuth client for a provider.

    Raises:
        RuntimeError: If OAuth plugin not enabled
    """
    app = get_current_app()
    if not app.oauth:
        raise RuntimeError("OAuth plugin not enabled")
    return app.oauth.get_client(provider.value)


# =============================================================================
# Session cookie
# =============================================================================


def set_session_cookie(
    response: Response,
    session_id: str,
    session_config: SessionConfig,
    *,
    max_age_seconds: int | None = None,
) -> None:
    """Set session cookie on response with consistent configuration.

    Pass empty string as session_id and max_age_seconds=0 to clear.
    """
    if max_age_seconds is None:
        max_age_seconds = 86400 * session_config.max_lifetime_days

    response.set_cookie(
        key=session_config.cookie_name,
        value=session_id,
        domain=session_config.cookie_domain or None,
        path="/",
        httponly=session_config.cookie_httponly,
        secure=session_config.cookie_secure,
        samesite=cast(Literal["lax", "strict", "none"], session_config.cookie_samesite),
        max_age=max_age_seconds,
    )


# =============================================================================
# GitHub email extraction
# =============================================================================


def _pick_github_email(userinfo: dict, emails: list[dict]) -> str | None:
    """Pick the best email from GitHub userinfo and /user/emails response.

    Priority: primary verified non-noreply > any verified non-noreply > userinfo > noreply fallback.

    The noreply address is skipped in the first two passes because GitHub sets it as the
    primary when "Keep my email address private" is enabled — we want the real address so
    that merging with an existing user (e.g. GitLab) works correctly.
    """
    for entry in emails:
        addr = entry.get("email", "")
        if (
            entry.get("primary")
            and entry.get("verified")
            and not addr.endswith("@users.noreply.github.com")
        ):
            return addr

    for entry in emails:
        addr = entry.get("email", "")
        if entry.get("verified") and not addr.endswith("@users.noreply.github.com"):
            return addr

    if userinfo.get("email"):
        return userinfo["email"]

    login = userinfo.get("login")
    return f"{login}@users.noreply.github.com" if login else None


def _all_verified_github_emails(userinfo: dict, emails: list[dict]) -> list[str]:
    """Return all verified non-noreply emails from GitHub, primary first."""
    result = []
    # Primary first
    for entry in emails:
        addr = entry.get("email", "")
        if (
            entry.get("primary")
            and entry.get("verified")
            and not addr.endswith("@users.noreply.github.com")
        ):
            result.append(addr)
    # Then the rest
    for entry in emails:
        addr = entry.get("email", "")
        if (
            not entry.get("primary")
            and entry.get("verified")
            and not addr.endswith("@users.noreply.github.com")
        ):
            result.append(addr)
    return result


async def _extract_github_user(token: dict) -> tuple[dict, str | None, list[str]]:
    """Extract userinfo, primary email, and all verified emails for a GitHub user.

    Returns (userinfo, primary_email, all_verified_emails).
    all_verified_emails is used for cross-provider user merging — a user may share
    a secondary GitHub email with their GitLab primary email.
    """
    app = get_current_app()
    github = app.github
    access_token = token.get("access_token")
    userinfo = token.get("userinfo", {})

    if not userinfo and github and access_token:
        userinfo = await github.fetch_user_info(access_token)

    # Fetch emails via GitHub API (userinfo email is often null for private emails)
    emails: list[dict] = []
    if github and access_token:
        try:
            emails = await github.fetch_user_emails(access_token)
        except Exception as e:
            logger.warning(f"Failed to fetch GitHub emails: {e}")

    email = _pick_github_email(userinfo, emails)
    all_emails = _all_verified_github_emails(userinfo, emails)
    return userinfo, email, all_emails


def _extract_generic_user(token: dict) -> tuple[dict, str | None]:
    """Extract userinfo and email for GitLab users."""
    userinfo = token.get("userinfo", {})
    email = userinfo.get("email")
    return userinfo, email


# =============================================================================
# OAuth user creation
# =============================================================================


async def handle_oauth_user_creation(
    provider: OAuthProvider,
    token: dict,
) -> User:
    """Handle OAuth user creation/update after successful authentication.

    Extracts user identity from the OAuth token, creates or updates the user
    and provider identity, and queues a membership sync job.
    """
    all_emails: list[str] = []
    if provider == OAuthProvider.GITHUB:
        userinfo, email, all_emails = await _extract_github_user(token)
    else:
        userinfo, email = _extract_generic_user(token)

    external_id = userinfo.get("id")
    if not external_id:
        raise ValueError(f"No user ID found in {provider.value} OAuth response")

    avatar_url = userinfo.get("avatar_url") or userinfo.get("picture")
    username = (
        userinfo.get("login")
        or userinfo.get("username")
        or userinfo.get("preferred_username")
        or userinfo.get("name")
    )
    if not username:
        raise ValueError(f"No username found in {provider.value} OAuth response")

    if not email:
        raise ValueError(f"No email found in {provider.value} OAuth response")

    app = get_current_app()
    if not app.database:
        raise RuntimeError("Database plugin not available")

    with app.database.session() as db:
        user, _ = db_create_or_update_user(
            db=db,
            provider=provider.value,
            external_id=str(external_id),
            email=email,
            username=username,
            avatar_url=avatar_url,
            candidate_emails=all_emails,
        )

    # Queue membership sync for GitHub and GitLab
    if provider in (OAuthProvider.GITHUB, OAuthProvider.GITLAB):
        oauth_access_token = token.get("access_token")
        if oauth_access_token and app.database:
            try:
                encrypted_token = app.database.encrypt(oauth_access_token)
                broker = get_faststream_broker()
                message = SyncUserMembershipsMessage(
                    user_id=str(user.id),
                    provider=provider.value,
                    access_token_encrypted=encrypted_token,
                    external_user_id=str(external_id),
                    username=username,
                )
                await broker.publish(
                    message,
                    stream="jeanclode.events.auth.sync_memberships",
                    maxlen=STREAM_MAXLEN,
                )
                logger.info(f"Queued membership sync for user {user.id} ({provider.value})")
            except Exception as e:
                logger.error(f"Failed to queue membership sync: {e}", exc_info=True)

    return user
