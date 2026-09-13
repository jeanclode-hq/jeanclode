"""Web server plugin configuration."""

from pydantic import BaseModel, Field


class CORSConfig(BaseModel):
    """CORS configuration for the web server."""

    allow_credentials: bool = True
    allow_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
    )
    allow_headers: list[str] = Field(
        default_factory=lambda: ["content-type", "authorization", "x-requested-with"]
    )
    additional_dev_origins: list[str] = Field(default_factory=list)


class SessionConfig(BaseModel):
    """Server-side session configuration (Redis-backed)."""

    cookie_name: str = "jeanclode_session"
    cookie_domain: str | None = None
    cookie_httponly: bool = True
    cookie_samesite: str = "lax"
    cookie_secure: bool = True
    inactivity_timeout_hours: int = 24
    max_lifetime_days: int = 30
    oauth_state_secret: str = "change-me-in-production"


class AdminConfig(BaseModel):
    """Admin surface configuration — setup wizard and admin page.

    ``secret`` must be at least 32 characters. The app refuses to start with
    a shorter value (pydantic validation fails).
    """

    secret: str = Field(..., min_length=32)
    session_ttl_seconds: int = 3600
    rate_limit_max_attempts: int = 5
    rate_limit_window_seconds: int = 900
    cookie_name: str = "jeanclode_admin"


class WebPluginConfig(BaseModel):
    """Web server plugin configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    # Skip starting uvicorn server (useful for tests with TestClient)
    skip_server: bool = False

    # Frontend URL for CORS
    frontend_url: str = "http://localhost:3000"

    # Enable when running behind a reverse proxy
    behind_proxy: bool = False

    # CORS configuration
    cors: CORSConfig = Field(default_factory=CORSConfig)

    # Session configuration
    session: SessionConfig = Field(default_factory=SessionConfig)

    # Admin surface configuration (setup wizard + admin page)
    admin: AdminConfig
