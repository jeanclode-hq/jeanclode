"""Tests for the Sentry webhook endpoint."""

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock, patch

from api.database import db_create_org
from api.models import Base

PAYLOAD = b'{"action": "created", "data": {"issue": {"id": "123"}}, "installation": {"uuid": "test-install-uuid"}}'
SECRET = "test-sentry-secret"

BROKER_PATH = "api.routers.webhooks.sentry.route.get_faststream_broker"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _create_org_with_secret(app):
    """Create an Organization (sentry) in DB with the test secret for webhook verification."""
    db_plugin = app.database
    Base.metadata.create_all(bind=db_plugin.engine)
    encrypted_secret = db_plugin.encrypt(SECRET)
    with db_plugin.session() as db:
        db_create_org(
            db,
            workspace_id=None,
            name="test-org",
            external_org_id="test-org",
            provider="sentry",
            installation_id="test-install-uuid",
            client_secret_encrypted=encrypted_secret,
        )


def test_valid_webhook_returns_200(auth_client, app) -> None:
    """Valid signature + known installation → 200."""
    _create_org_with_secret(app)
    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock()

    with patch(BROKER_PATH, return_value=mock_broker):
        resp = auth_client.post(
            "/webhooks/sentry",
            content=PAYLOAD,
            headers={"sentry-hook-signature": _sign(PAYLOAD)},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_broker.publish.assert_awaited_once()
    call_kwargs = mock_broker.publish.call_args
    assert call_kwargs.kwargs.get("stream") == "jeanclode.events.sentry.webhooks"


def test_invalid_signature_returns_401(auth_client, app) -> None:
    """Bad signature → 401."""
    _create_org_with_secret(app)

    resp = auth_client.post(
        "/webhooks/sentry",
        content=PAYLOAD,
        headers={"sentry-hook-signature": "bad-sig"},
    )
    assert resp.status_code == 401


def test_unknown_installation_wrong_secret_returns_401(auth_client, app) -> None:
    """Unknown installation with wrong secret → 401."""
    _create_org_with_secret(app)

    unknown_payload = b'{"action": "created", "installation": {"uuid": "unknown-uuid"}}'
    # Sign with a different secret — no org has this secret
    bad_sig = _sign(unknown_payload, secret="wrong-secret")
    resp = auth_client.post(
        "/webhooks/sentry",
        content=unknown_payload,
        headers={"sentry-hook-signature": bad_sig},
    )
    assert resp.status_code == 401


def test_missing_signature_header_returns_422(auth_client, app) -> None:
    resp = auth_client.post("/webhooks/sentry", content=PAYLOAD)
    assert resp.status_code == 422


def test_redis_failure_returns_503(auth_client, app) -> None:
    """Broker failure → 503."""
    _create_org_with_secret(app)
    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock(side_effect=ConnectionError("Redis down"))

    with patch(BROKER_PATH, return_value=mock_broker):
        resp = auth_client.post(
            "/webhooks/sentry",
            content=PAYLOAD,
            headers={"sentry-hook-signature": _sign(PAYLOAD)},
        )
    assert resp.status_code == 503
