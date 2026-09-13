"""Dependencies for the admin surface (admin page + setup wizard).

Both surfaces share a single auth mechanism: ``POST /admin/auth`` with the
``ADMIN_SECRET`` env var creates a short-lived Redis session and sets an
HTTP-only cookie. Subsequent calls are gated by ``require_admin``.
"""

import logging

from fastapi import Cookie, HTTPException

from api.context import get_current_app

logger = logging.getLogger(__name__)

ADMIN_SESSION_PREFIX = "admin:session:"


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


def require_admin_enabled() -> None:
    """Reject admin endpoints entirely when the instance is env-managed.

    When env vars fully configure both a git provider and the LLM, DB writes
    from the admin UI would be silently shadowed by env precedence. Block the
    endpoints instead of letting users save values that won't take effect.

    The decision is computed once at startup and cached on ``Application``.
    """
    if not get_current_app().admin_enabled:
        raise HTTPException(
            status_code=403,
            detail="Admin UI is disabled: this instance is managed via environment variables.",
        )


async def require_admin(jeanclode_admin: str | None = Cookie(None)) -> None:
    """Raise 401 unless a valid admin session cookie is present."""
    require_admin_enabled()
    admin_cfg = _get_admin_config()
    cookie_value = jeanclode_admin  # param name matches cookie name
    if not cookie_value:
        # Fall back to configured cookie name if different from param default.
        # FastAPI binds by parameter name, so this only matters if the cookie
        # name is customised — we keep the parameter named ``jeanclode_admin``
        # to match the default.
        raise HTTPException(status_code=401, detail="Admin authentication required")

    redis = _get_redis()
    key = f"{ADMIN_SESSION_PREFIX}{cookie_value}"
    exists = await redis.get(key)
    if not exists:
        raise HTTPException(status_code=401, detail="Admin session expired or invalid")

    # Sliding-window refresh
    await redis.expire(key, admin_cfg.session_ttl_seconds)
