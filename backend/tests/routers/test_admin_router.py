"""Tests for the admin router (auth + settings CRUD + GitHub manifest flow)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.services.instance_settings import (
    load_github_config,
    load_gitlab_config,
    save_github_config,
    save_gitlab_config,
)
from api.services.llm_credentials import list_llm_credentials
from tests.utils.fake_redis import FakeRedis


@pytest.fixture
def fake_redis(app):
    fake = FakeRedis()
    with patch.object(app.faststream, "get_redis", return_value=fake):
        yield fake


@pytest.fixture
def admin_client(app, db_session, fake_redis):
    web = app.web
    assert web is not None
    tc = TestClient(web.get_asgi_app())
    return tc


def _authenticate(admin_client: TestClient, secret: str) -> None:
    resp = admin_client.post("/admin/auth", json={"secret": secret})
    assert resp.status_code == 200


def _test_secret(app) -> str:
    return app.web.config.admin.secret


def test_auth_valid_secret_sets_cookie(admin_client, app):
    resp = admin_client.post("/admin/auth", json={"secret": _test_secret(app)})
    assert resp.status_code == 200
    assert "jeanclode_admin" in resp.cookies


def test_auth_invalid_secret_returns_401(admin_client):
    resp = admin_client.post("/admin/auth", json={"secret": "wrong-secret"})
    assert resp.status_code == 401
    assert "jeanclode_admin" not in resp.cookies


def test_auth_rate_limit_after_repeated_failures(admin_client, app):
    # 5 failed attempts are allowed before the rate limiter kicks in.
    max_attempts = app.web.config.admin.rate_limit_max_attempts
    for _ in range(max_attempts):
        r = admin_client.post("/admin/auth", json={"secret": "nope"})
        assert r.status_code == 401
    # Next attempt (even a valid one) is now blocked.
    r = admin_client.post("/admin/auth", json={"secret": _test_secret(app)})
    assert r.status_code == 429


def test_get_settings_requires_auth(admin_client):
    resp = admin_client.get("/admin/settings")
    assert resp.status_code == 401


def test_get_settings_masks_secrets(admin_client, app, db_session):
    save_github_config(
        db_session,
        {
            "client_id": "Iv1.abc",
            "client_secret": "super-secret",
            "app_id": "12345",
            "private_key_pem": "pem",
            "webhook_secret": "hooky",
            "name": "jeanclode",
        },
    )
    _authenticate(admin_client, _test_secret(app))

    resp = admin_client.get("/admin/settings")
    assert resp.status_code == 200
    gh = resp.json()["github"]
    # Non-secret fields are returned in full.
    assert gh["client_id"] == "Iv1.abc"
    assert gh["app_id"] == "12345"
    assert gh["name"] == "jeanclode"
    # Secret fields are masked.
    assert gh["client_secret"] == "****"
    assert gh["private_key_pem"] == "****"
    assert gh["webhook_secret"] == "****"


def test_put_github_persists_config(admin_client, app, db_session):
    _authenticate(admin_client, _test_secret(app))

    resp = admin_client.put(
        "/admin/settings/github",
        json={
            "client_id": "Iv1.xyz",
            "client_secret": "new-secret",
            "app_id": "99",
            "private_key_pem": "pem-body",
            "webhook_secret": "hook",
            "name": "jeanclode",
        },
    )
    assert resp.status_code == 200

    loaded = load_github_config(db_session)
    assert loaded is not None
    assert loaded["client_id"] == "Iv1.xyz"
    assert loaded["client_secret"] == "new-secret"


def test_put_gitlab_persists_config(admin_client, app, db_session):
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.put(
        "/admin/settings/gitlab",
        json={
            "client_id": "gl-id",
            "client_secret": "gl-secret",
            "instance_url": "https://gitlab.com",
        },
    )
    assert resp.status_code == 200

    loaded = load_gitlab_config(db_session)
    assert loaded is not None
    assert loaded["client_id"] == "gl-id"


def test_post_llm_credential_rejects_openai_compatible_without_base_url(admin_client, app):
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.post(
        "/admin/llm-credentials",
        json={
            "kind": "api_key",
            "provider": "openai_compatible",
            "secret": "sk-xxx",
            "model_high": "custom-model",
            "model_low": "custom-model-mini",
            "base_url": None,
        },
    )
    assert resp.status_code == 422


def test_post_llm_credential_accepts_anthropic(admin_client, app, db_session):
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.post(
        "/admin/llm-credentials",
        json={
            "kind": "api_key",
            "provider": "anthropic",
            "secret": "sk-ant-xxx",
            "model_high": "claude-opus-4-6",
            "model_low": "claude-haiku-4-5",
            "base_url": None,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "anthropic"
    assert body["secret"] == "****"
    assert body["priority"] == 1

    credentials = list_llm_credentials(db_session)
    assert len(credentials) == 1
    assert credentials[0].provider == "anthropic"


def test_get_llm_credentials_lists_ordered_by_priority(admin_client, app):
    _authenticate(admin_client, _test_secret(app))
    for i in range(2):
        resp = admin_client.post(
            "/admin/llm-credentials",
            json={
                "kind": "api_key",
                "provider": "anthropic",
                "secret": f"sk-ant-{i}",
                "model_high": "claude-opus-4-6",
                "model_low": "claude-haiku-4-5",
            },
        )
        assert resp.status_code == 200

    resp = admin_client.get("/admin/llm-credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["priority"] for c in body] == [1, 2]


def test_delete_llm_credential_removes_it(admin_client, app):
    _authenticate(admin_client, _test_secret(app))
    created = admin_client.post(
        "/admin/llm-credentials",
        json={
            "kind": "api_key",
            "provider": "anthropic",
            "secret": "sk-ant-xxx",
            "model_high": "claude-opus-4-6",
            "model_low": "claude-haiku-4-5",
        },
    ).json()

    resp = admin_client.delete(f"/admin/llm-credentials/{created['id']}")
    assert resp.status_code == 200
    assert admin_client.get("/admin/llm-credentials").json() == []


def test_reorder_llm_credentials(admin_client, app):
    _authenticate(admin_client, _test_secret(app))
    ids = []
    for i in range(2):
        created = admin_client.post(
            "/admin/llm-credentials",
            json={
                "kind": "api_key",
                "provider": "anthropic",
                "secret": f"sk-ant-{i}",
                "model_high": "claude-opus-4-6",
                "model_low": "claude-haiku-4-5",
            },
        ).json()
        ids.append(created["id"])

    resp = admin_client.post(
        "/admin/llm-credentials/reorder",
        json={"ordered_ids": list(reversed(ids))},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body] == list(reversed(ids))
    assert [c["priority"] for c in body] == [1, 2]


def test_delete_category_removes_rows(admin_client, app, db_session):
    save_gitlab_config(
        db_session,
        {"client_id": "a", "client_secret": "b", "instance_url": "https://gitlab.com"},
    )
    _authenticate(admin_client, _test_secret(app))

    resp = admin_client.delete("/admin/settings/gitlab")
    assert resp.status_code == 200
    assert load_gitlab_config(db_session) is None


def test_logout_clears_cookie_and_session(admin_client, app, fake_redis):
    _authenticate(admin_client, _test_secret(app))
    # Sanity: a session row was created.
    assert any(k.startswith("admin:session:") for k in fake_redis.store)

    resp = admin_client.post("/admin/logout")
    assert resp.status_code == 200
    # After logout the session store is empty of admin sessions.
    assert not any(k.startswith("admin:session:") for k in fake_redis.store)


# =============================================================================
# GitHub App manifest flow
# =============================================================================


def test_manifest_requires_auth(app):
    """Without the admin cookie the manifest endpoint rejects the request."""
    web = app.web
    assert web is not None
    tc = TestClient(web.get_asgi_app())
    resp = tc.get("/admin/github/manifest")
    assert resp.status_code == 401


def test_manifest_returns_state_and_stores_in_redis(admin_client, app, fake_redis):
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.get(
        "/admin/github/manifest",
        params={"public_url": "https://example.ngrok-free.app"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "manifest" in body
    assert isinstance(body["state"], str) and len(body["state"]) > 16
    # Webhook URL must be publicly reachable, not localhost.
    assert body["manifest"]["hook_attributes"]["url"].startswith("https://example.ngrok-free.app")
    # The CSRF state is persisted in Redis for later validation.
    assert any(k == f"github:manifest:state:{body['state']}" for k in fake_redis.store)


def test_manifest_rejects_localhost_public_url(admin_client, app):
    """GitHub refuses manifests pointing webhooks at localhost."""
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.get(
        "/admin/github/manifest",
        params={"public_url": "http://localhost:8000"},
    )
    assert resp.status_code == 400


def test_manifest_callback_rejects_tampered_state(admin_client, app):
    _authenticate(admin_client, _test_secret(app))
    resp = admin_client.get(
        "/admin/github/manifest/callback",
        params={"code": "gh_code_123", "state": "forged-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_manifest_callback_saves_and_redirects(admin_client, app, fake_redis, db_session):
    _authenticate(admin_client, _test_secret(app))
    # Seed a valid manifest state so the CSRF check passes.
    fake_redis.store["github:manifest:state:valid-state"] = b"1"

    fake_response = MagicMock()
    fake_response.status_code = 201
    fake_response.json.return_value = {
        "id": 424242,
        "name": "jeanclode-prod",
        "client_id": "Iv1.manifest",
        "client_secret": "manifest-secret",
        "pem": "-----BEGIN PRIVATE KEY-----\npem\n-----END PRIVATE KEY-----",
        "webhook_secret": "wh-secret",
    }
    fake_post = AsyncMock(return_value=fake_response)

    with patch("httpx.AsyncClient.post", fake_post):
        resp = admin_client.get(
            "/admin/github/manifest/callback",
            params={"code": "gh_code_123", "state": "valid-state"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/admin")
    # The CSRF state must be consumed on use.
    assert "github:manifest:state:valid-state" not in fake_redis.store
    # Credentials got persisted to the DB.
    github = load_github_config(db_session)
    assert github is not None
    assert github["client_id"] == "Iv1.manifest"
    assert github["app_id"] == "424242"
