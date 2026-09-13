"""Tests for GitHub installation webhook handlers."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from api.models import Base


def _make_installation_payload(action, installation_id="123", org_id="456", org_name="test-org"):
    """Build a GitHub App installation webhook payload."""
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": {
                "id": org_id,
                "login": org_name,
                "avatar_url": "https://github.com/avatar.png",
            },
        },
        "repositories": [
            {"id": 789, "name": "repo-1", "html_url": "https://github.com/test-org/repo-1"}
        ],
        "sender": {"id": 100, "login": "admin-user"},
    }


def _make_repos_payload(action, installation_id="123", repos_added=None, repos_removed=None):
    """Build a GitHub installation_repositories webhook payload."""
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": {"id": 456, "login": "test-org"},
        },
        "repositories_added": repos_added,
        "repositories_removed": repos_removed,
        "sender": {"id": 100, "login": "admin-user"},
    }


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for GitHub webhook."""
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


@pytest.fixture
def github_app(app):
    """App fixture with GitHub plugin enabled and tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    # Mock the GitHub plugin onto the app
    from api.plugins.github.config import GitHubAppConfig, GitHubPluginConfig
    from api.plugins.github.plugin import GitHubPlugin

    github_config = GitHubPluginConfig(
        enabled=True,
        app=GitHubAppConfig(
            client_id="test-client-id",
            client_secret="test-client-secret",
            app_id="12345",
            private_key_path="/tmp/fake-key.pem",
            webhook_secret="test-webhook-secret",
            name="test-app",
        ),
    )
    github_plugin = GitHubPlugin(github_config)
    app._plugins.append(github_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(github_plugin)


@pytest.fixture
def github_client(github_app):
    """Test client with GitHub plugin enabled."""
    from fastapi.testclient import TestClient

    return TestClient(github_app.web.get_asgi_app())


WEBHOOK_SECRET = "test-webhook-secret"


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_installation_created(mock_broker, github_client):
    """installation.created creates org and queues repo sync."""
    mock_broker.return_value = AsyncMock()

    payload = _make_installation_payload("created")
    body = json.dumps(payload).encode()
    signature = _sign_payload(body, WEBHOOK_SECRET)

    response = github_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["processed"] is True
    assert "test-org" in data["message"]


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_installation_created_idempotent(mock_broker, github_client):
    """Replaying installation.created is idempotent."""
    mock_broker.return_value = AsyncMock()

    payload = _make_installation_payload("created")
    body = json.dumps(payload).encode()
    signature = _sign_payload(body, WEBHOOK_SECRET)
    headers = {
        "X-GitHub-Event": "installation",
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }

    # First call creates
    response1 = github_client.post("/webhooks/github", content=body, headers=headers)
    assert response1.status_code == 202
    assert response1.json()["processed"] is True

    # Second call is idempotent
    response2 = github_client.post("/webhooks/github", content=body, headers=headers)
    assert response2.status_code == 202
    assert response2.json()["processed"] is True
    assert "already exists" in response2.json()["message"]


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_installation_reinstall_updates_installation_id(mock_broker, github_client):
    """Reinstalling (new installation_id, same org) updates existing git org."""
    mock_broker.return_value = AsyncMock()

    # First install
    payload1 = _make_installation_payload(
        "created", installation_id="100", org_id="456", org_name="test-org"
    )
    body1 = json.dumps(payload1).encode()
    sig1 = _sign_payload(body1, WEBHOOK_SECRET)
    response1 = github_client.post(
        "/webhooks/github",
        content=body1,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": sig1,
            "Content-Type": "application/json",
        },
    )
    assert response1.status_code == 202
    assert response1.json()["processed"] is True

    # Reinstall — new installation_id, same org_id
    payload2 = _make_installation_payload(
        "created", installation_id="200", org_id="456", org_name="test-org"
    )
    body2 = json.dumps(payload2).encode()
    sig2 = _sign_payload(body2, WEBHOOK_SECRET)
    response2 = github_client.post(
        "/webhooks/github",
        content=body2,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": sig2,
            "Content-Type": "application/json",
        },
    )
    assert response2.status_code == 202
    assert response2.json()["processed"] is True


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_installation_deleted(mock_broker, github_client):
    """installation.deleted removes the org."""
    mock_broker.return_value = AsyncMock()

    # First create
    create_payload = _make_installation_payload("created")
    create_body = json.dumps(create_payload).encode()
    create_sig = _sign_payload(create_body, WEBHOOK_SECRET)
    github_client.post(
        "/webhooks/github",
        content=create_body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": create_sig,
            "Content-Type": "application/json",
        },
    )

    # Then delete
    delete_payload = _make_installation_payload("deleted")
    delete_body = json.dumps(delete_payload).encode()
    delete_sig = _sign_payload(delete_body, WEBHOOK_SECRET)
    response = github_client.post(
        "/webhooks/github",
        content=delete_body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": delete_sig,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["processed"] is True
    assert "deleted" in data["message"]


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_repositories_added(mock_broker, github_client):
    """installation_repositories added creates repos."""
    mock_broker.return_value = AsyncMock()

    # First create the org
    create_payload = _make_installation_payload("created")
    create_body = json.dumps(create_payload).encode()
    create_sig = _sign_payload(create_body, WEBHOOK_SECRET)
    github_client.post(
        "/webhooks/github",
        content=create_body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": create_sig,
            "Content-Type": "application/json",
        },
    )

    # Then add repos
    repos_payload = _make_repos_payload(
        "added",
        repos_added=[
            {"id": 1001, "name": "new-repo", "html_url": "https://github.com/test-org/new-repo"},
        ],
    )
    repos_body = json.dumps(repos_payload).encode()
    repos_sig = _sign_payload(repos_body, WEBHOOK_SECRET)
    response = github_client.post(
        "/webhooks/github",
        content=repos_body,
        headers={
            "X-GitHub-Event": "installation_repositories",
            "X-Hub-Signature-256": repos_sig,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["processed"] is True
    assert "Added 1" in data["message"]


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_repositories_removed(mock_broker, github_client):
    """installation_repositories removed deletes repos."""
    mock_broker.return_value = AsyncMock()

    # Create org
    create_payload = _make_installation_payload("created")
    create_body = json.dumps(create_payload).encode()
    create_sig = _sign_payload(create_body, WEBHOOK_SECRET)
    github_client.post(
        "/webhooks/github",
        content=create_body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": create_sig,
            "Content-Type": "application/json",
        },
    )

    # Add a repo
    add_payload = _make_repos_payload(
        "added",
        repos_added=[
            {"id": 2001, "name": "to-remove", "html_url": "https://github.com/test-org/to-remove"},
        ],
    )
    add_body = json.dumps(add_payload).encode()
    add_sig = _sign_payload(add_body, WEBHOOK_SECRET)
    github_client.post(
        "/webhooks/github",
        content=add_body,
        headers={
            "X-GitHub-Event": "installation_repositories",
            "X-Hub-Signature-256": add_sig,
            "Content-Type": "application/json",
        },
    )

    # Remove the repo
    remove_payload = _make_repos_payload(
        "removed",
        repos_removed=[{"id": 2001, "name": "to-remove"}],
    )
    remove_body = json.dumps(remove_payload).encode()
    remove_sig = _sign_payload(remove_body, WEBHOOK_SECRET)
    response = github_client.post(
        "/webhooks/github",
        content=remove_body,
        headers={
            "X-GitHub-Event": "installation_repositories",
            "X-Hub-Signature-256": remove_sig,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["processed"] is True
    assert "Removed 1" in data["message"]


def test_invalid_signature(github_client):
    """Invalid HMAC signature returns 401."""
    payload = _make_installation_payload("created")
    body = json.dumps(payload).encode()
    bad_signature = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

    response = github_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": bad_signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
