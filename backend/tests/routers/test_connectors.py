"""Unit tests for the connectors route logic that don't need a live DB.

Calls route handlers directly with mocked ``db``/``current_user`` — same
style as ``tests/plugins/test_dispatch_inputs.py`` — rather than going
through ``TestClient``, since the sandbox this was written in has no
Postgres available. Full HTTP-level coverage (auth, DB round-trips)
belongs in a ``TestClient``-based suite once that's runnable here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.models.connectors import AuthType, SubjectType
from api.routers.connectors.route import (
    _reject_reserved_mcp_server_name,
    create_mcp_server,
    update_mcp_server,
    write_credential,
)
from api.routers.connectors.schemas import (
    CreateMcpServerRequest,
    UpdateMcpServerRequest,
    WriteCredentialRequest,
)


def _make_org(provider: str = "github") -> MagicMock:
    org = MagicMock()
    org.provider = provider
    return org


@pytest.mark.parametrize("name", ["memory", "Memory", " MEMORY ", "memory "])
def test_reject_reserved_mcp_server_name_rejects_case_and_whitespace_variants(name: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _reject_reserved_mcp_server_name(name)
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("name", [None, "outline", "not-memory", "memory-tool"])
def test_reject_reserved_mcp_server_name_allows_everything_else(name: str | None) -> None:
    _reject_reserved_mcp_server_name(name)  # must not raise


def test_create_mcp_server_rejects_reserved_name() -> None:
    """Regression test: an org MCP server named "memory" would silently
    overwrite the CLI's built-in memory MCP server in the same
    ClaudeAgentOptions.mcp_servers dict (see cli/src/agents/base.py) —
    reject it at creation instead."""
    request = CreateMcpServerRequest(org_id=uuid4(), name="memory", host="https://mcp.example.com")
    db = MagicMock()

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch("api.routers.connectors.route.db_get_org_by_id", return_value=_make_org()),
        pytest.raises(HTTPException) as exc_info,
    ):
        create_mcp_server(request, current_user=MagicMock(), db=db)

    assert exc_info.value.status_code == 422
    db.add.assert_not_called()


def test_update_mcp_server_rejects_reserved_name() -> None:
    request = UpdateMcpServerRequest(name="memory")
    db = MagicMock()
    existing = MagicMock()
    existing.org_id = uuid4()

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch("api.routers.connectors.route.db_get_mcp_server_by_id", return_value=existing),
        patch("api.routers.connectors.route.db_update_mcp_server") as mock_update,
        pytest.raises(HTTPException) as exc_info,
    ):
        update_mcp_server(uuid4(), request, current_user=MagicMock(), db=db)

    assert exc_info.value.status_code == 422
    mock_update.assert_not_called()


def test_create_mcp_server_allows_non_reserved_name() -> None:
    request = CreateMcpServerRequest(org_id=uuid4(), name="outline", host="https://mcp.example.com")
    db = MagicMock()
    # "name" is special-cased by the MagicMock constructor (sets the mock's
    # repr, not an attribute) — must be assigned after construction.
    created = MagicMock(id=uuid4(), org_id=request.org_id, host=request.host)
    created.name = "outline"

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch("api.routers.connectors.route.db_get_org_by_id", return_value=_make_org()),
        patch(
            "api.routers.connectors.route.db_create_mcp_server", return_value=created
        ) as mock_create,
    ):
        response = create_mcp_server(request, current_user=MagicMock(), db=db)

    mock_create.assert_called_once()
    assert response.name == "outline"
    assert response.credential_count == 0


def _oauth2_write_request(**settings_overrides: object) -> WriteCredentialRequest:
    settings = {"token_url": "https://auth.example.com/token", "grant_type": "client_credentials"}
    settings.update(settings_overrides)
    return WriteCredentialRequest(
        org_id=uuid4(),
        subject_type=SubjectType.MCP_SERVER,
        subject_id=uuid4(),
        auth_type=AuthType.OAUTH2,
        secret={"client_id": "cid", "client_secret": "csec"},
        settings=settings,
    )


def _make_mcp_server_for_credential(org_id: object) -> MagicMock:
    server = MagicMock()
    server.org_id = org_id
    return server


@pytest.mark.parametrize(
    ("grant_type", "extra_secret"),
    [
        ("password", {}),
        ("password", {"username": "bob"}),  # missing password
        ("refresh_token", {}),
    ],
)
def test_write_credential_oauth2_rejects_missing_grant_specific_fields(
    grant_type: str, extra_secret: dict[str, str]
) -> None:
    request = _oauth2_write_request(grant_type=grant_type)
    request.secret.update(extra_secret)
    db = MagicMock()

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch(
            "api.routers.connectors.route.db_get_mcp_server_by_id",
            return_value=_make_mcp_server_for_credential(request.org_id),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        write_credential(request, current_user=MagicMock(), db=db)

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    ("grant_type", "extra_secret"),
    [
        ("client_credentials", {}),
        ("password", {"username": "bob", "password": "pw"}),
        ("refresh_token", {"refresh_token": "rt-1"}),
    ],
)
def test_write_credential_oauth2_accepts_grant_with_required_fields(
    grant_type: str, extra_secret: dict[str, str]
) -> None:
    request = _oauth2_write_request(grant_type=grant_type)
    request.secret.update(extra_secret)
    db = MagicMock()
    app = MagicMock()
    app.database.encrypt.return_value = "encrypted-blob"
    saved = MagicMock(
        id=uuid4(),
        org_id=request.org_id,
        subject_type=request.subject_type.value,
        subject_id=request.subject_id,
        auth_type=request.auth_type.value,
        settings=request.settings,
    )

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch(
            "api.routers.connectors.route.db_get_mcp_server_by_id",
            return_value=_make_mcp_server_for_credential(request.org_id),
        ),
        patch("api.routers.connectors.route.get_current_app", return_value=app),
        patch("api.routers.connectors.route.db_get_credentials_by_subject", return_value=[]),
        patch(
            "api.routers.connectors.route.db_create_credential", return_value=saved
        ) as mock_create,
    ):
        response = write_credential(request, current_user=MagicMock(), db=db)

    mock_create.assert_called_once()
    assert response.auth_type == AuthType.OAUTH2


def _stored(org_id: object, subject_id: object, *, auth_type: str, host: str | None) -> MagicMock:
    return MagicMock(
        id=uuid4(),
        org_id=org_id,
        subject_type=SubjectType.PLUGIN_INSTALLATION.value,
        subject_id=subject_id,
        auth_type=auth_type,
        settings={"host": host} if host else {},
    )


def _skill_request(auth_type: AuthType, secret: dict, settings: dict, **extra: object):
    return WriteCredentialRequest(
        org_id=uuid4(),
        subject_type=SubjectType.PLUGIN_INSTALLATION,
        subject_id=uuid4(),
        auth_type=auth_type,
        secret=secret,
        settings=settings,
        **extra,
    )


def _write(request: WriteCredentialRequest, existing: list[MagicMock]) -> dict[str, MagicMock]:
    """Run write_credential against an install owning ``existing``."""
    install = MagicMock(org_id=request.org_id)
    app = MagicMock()
    app.database.encrypt.return_value = "encrypted-blob"

    def saved(*_args: object, **kwargs: object) -> MagicMock:
        return MagicMock(
            id=uuid4(),
            org_id=request.org_id,
            subject_type=request.subject_type.value,
            subject_id=request.subject_id,
            auth_type=kwargs["auth_type"],
            settings=kwargs["settings"],
        )

    with (
        patch("api.routers.connectors.route.verify_org_access_from_body"),
        patch("api.routers.connectors.route.db_get_installation_by_id", return_value=install),
        patch("api.routers.connectors.route.get_current_app", return_value=app),
        patch("api.routers.connectors.route.db_get_credentials_by_subject", return_value=existing),
        patch("api.routers.connectors.route.db_create_credential", side_effect=saved) as create,
        patch("api.routers.connectors.route.db_replace_credential", side_effect=saved) as replace,
    ):
        write_credential(request, current_user=MagicMock(), db=MagicMock())
    return {"create": create, "replace": replace}


def test_write_credential_adds_a_second_auth_on_another_host() -> None:
    request = _skill_request(AuthType.NONE, {}, {"host": "*.s3.amazonaws.com"})
    first = _stored(request.org_id, request.subject_id, auth_type="api_key", host="api.figma.com")

    calls = _write(request, [first])

    calls["create"].assert_called_once()
    calls["replace"].assert_not_called()


def test_write_credential_rejects_a_second_auth_on_the_same_host() -> None:
    request = _skill_request(AuthType.NONE, {}, {"host": "https://API.figma.com/v1"})
    first = _stored(request.org_id, request.subject_id, auth_type="api_key", host="api.figma.com")

    with pytest.raises(HTTPException) as exc_info:
        _write(request, [first])

    assert exc_info.value.status_code == 409


def test_write_credential_replaces_the_given_credential_on_its_own_host() -> None:
    first = _stored(uuid4(), uuid4(), auth_type="api_key", host="api.figma.com")
    request = _skill_request(
        AuthType.API_KEY,
        {"name": "FIGMA_API_TOKEN", "key": "figd_new"},
        {"host": "api.figma.com", "header": "X-Figma-Token", "value_prefix": None},
        credential_id=first.id,
    )
    first.org_id, first.subject_id = request.org_id, request.subject_id

    calls = _write(request, [first])

    calls["replace"].assert_called_once()
    assert calls["replace"].call_args.args[1] is first
    calls["create"].assert_not_called()


def test_write_credential_unknown_credential_id_is_404() -> None:
    request = _skill_request(AuthType.NONE, {}, {"host": "pypi.org"}, credential_id=uuid4())

    with pytest.raises(HTTPException) as exc_info:
        _write(request, [])

    assert exc_info.value.status_code == 404


def test_write_credential_none_requires_a_host() -> None:
    request = _skill_request(AuthType.NONE, {}, {})

    with pytest.raises(HTTPException) as exc_info:
        _write(request, [])

    assert exc_info.value.status_code == 422


def test_write_credential_accepts_basic_auth_on_a_skill() -> None:
    request = _skill_request(
        AuthType.BASIC_AUTH, {"username": "u", "password": "p"}, {"host": "zoning.internal"}
    )

    calls = _write(request, [])

    calls["create"].assert_called_once()
