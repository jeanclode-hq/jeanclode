"""Schemas for MCP servers and the generic connector credential store.

See https://github.com/jeanclode-hq/jeanclode/issues/14 — ``auth_type``
determines the field shape, both what the frontend form renders and what
goes in ``settings`` (non-secret) vs the encrypted secret blob.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from api.models.connectors import AuthType, SubjectType

OAuth2GrantType = Literal["client_credentials", "password", "refresh_token"]

# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class CreateMcpServerRequest(BaseModel):
    org_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=500, description="URL Jeanclode connects out to")


class UpdateMcpServerRequest(BaseModel):
    name: str | None = None
    host: str | None = None


class McpServerResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    host: str
    credential_count: int = 0


# ---------------------------------------------------------------------------
# Credential secret/settings shapes, one pair per auth_type
# ---------------------------------------------------------------------------


class ApiKeySecret(BaseModel):
    """``api_key``/``jwt`` secret. ``name`` doubles as the env var name the
    CLI sets in the agent's env when the subject is a skill — see the issue.
    """

    name: str = Field(min_length=1, description="Env var name a skill reads this as")
    key: str = Field(min_length=1)


class ApiKeySettings(BaseModel):
    header: str = Field(default="Authorization")
    value_prefix: str | None = Field(default="Bearer ")
    host: str | None = Field(
        default=None,
        description=(
            "Target host the proxy injects this credential into. Required for a "
            "skill credential (plugin_installation) — a skill has no host column "
            "of its own. Ignored for an mcp_server credential, which already has "
            "one via McpServer.host. Accepts a leading wildcard (e.g. "
            "*.figma.com) to cover any subdomain of a given domain — the apex "
            "domain itself is not included and needs its own entry if needed."
        ),
    )


class BasicAuthSecret(BaseModel):
    name: str | None = Field(
        default=None,
        description="Optional env var a skill checks for; set to a placeholder, "
        "the proxy injects the real header",
    )
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class BasicAuthSettings(BaseModel):
    host: str | None = Field(default=None, description="See ApiKeySettings.host")


class NoneSecret(BaseModel):
    """``none`` stores no secret — the host is only allowlisted."""


class NoneSettings(BaseModel):
    host: str = Field(
        min_length=1,
        description="Host reachable from the sandbox with nothing injected (see ApiKeySettings.host)",
    )


class OAuth2Secret(BaseModel):
    """client_id/client_secret are always required — most providers expect
    client authentication on every grant type this proxy supports, even
    password/refresh_token. ``username``/``password``/``refresh_token``
    are grant-type-specific; ``write_credential`` checks the ones the
    request's ``grant_type`` needs are actually present (see the route's
    cross-field check — a pydantic validator here has no access to the
    sibling ``OAuth2Settings.grant_type``, validated as a separate model).
    """

    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    username: str | None = Field(default=None, description="Required for the password grant")
    password: str | None = Field(default=None, description="Required for the password grant")
    refresh_token: str | None = Field(
        default=None, description="Required for the refresh_token grant"
    )


class OAuth2Settings(BaseModel):
    token_url: str = Field(min_length=1, description="Provider's OAuth2 token endpoint")
    grant_type: OAuth2GrantType = Field(default="client_credentials")
    scope: str | None = None
    host: str | None = Field(
        default=None,
        description=(
            "Target host the minted token is injected into — distinct from "
            "token_url's host. See ApiKeySettings.host for the subject_type rule."
        ),
    )


_SECRET_MODELS: dict[AuthType, type[BaseModel]] = {
    AuthType.API_KEY: ApiKeySecret,
    AuthType.JWT: ApiKeySecret,
    AuthType.BASIC_AUTH: BasicAuthSecret,
    AuthType.OAUTH2: OAuth2Secret,
    AuthType.NONE: NoneSecret,
}

_SETTINGS_MODELS: dict[AuthType, type[BaseModel]] = {
    AuthType.API_KEY: ApiKeySettings,
    AuthType.JWT: ApiKeySettings,
    AuthType.BASIC_AUTH: BasicAuthSettings,
    AuthType.OAUTH2: OAuth2Settings,
    AuthType.NONE: NoneSettings,
}


def secret_model_for(auth_type: AuthType) -> type[BaseModel]:
    return _SECRET_MODELS[auth_type]


def settings_model_for(auth_type: AuthType) -> type[BaseModel]:
    return _SETTINGS_MODELS[auth_type]


# ---------------------------------------------------------------------------
# Credential read/write
# ---------------------------------------------------------------------------


class WriteCredentialRequest(BaseModel):
    """Narrow write route — token in, nothing back.

    ``secret``/``settings`` are validated against the model matching
    ``auth_type`` (see ``secret_model_for``/``settings_model_for``) inside
    the route handler, not via a discriminated union here, so a bad shape
    reports a clear per-field 422 instead of "no union member matched".
    """

    org_id: uuid.UUID
    subject_type: SubjectType
    subject_id: uuid.UUID
    credential_id: uuid.UUID | None = Field(
        default=None, description="Credential to replace; omitted, a new one is added"
    )
    auth_type: AuthType
    secret: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


class CredentialStatus(BaseModel):
    """What the frontend gets back — never the secret itself."""

    id: uuid.UUID
    org_id: uuid.UUID
    subject_type: SubjectType
    subject_id: uuid.UUID
    auth_type: AuthType
    settings: dict
    created_at: datetime
    updated_at: datetime
