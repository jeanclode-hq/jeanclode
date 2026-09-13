"""Tests for GitHub repository-arrival webhooks.

Two events can announce a repo after the installation sync has run:
``installation_repositories.added`` (thin payload — id, name, full_name and
nothing else) and ``repository.created`` (the full repo object, and the only
one an org-wide installation reliably sends). Both have to leave a usable row
behind and queue the PR/issue backfill, since neither carries either.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_get_repository_by_external_id
from api.models import Base

WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def github_app(app):
    """App fixture with GitHub plugin enabled and tables created."""
    db = app.database
    Base.metadata.create_all(bind=db.engine)

    from api.plugins.github.config import GitHubAppConfig, GitHubPluginConfig
    from api.plugins.github.plugin import GitHubPlugin

    github_plugin = GitHubPlugin(
        GitHubPluginConfig(
            enabled=True,
            app=GitHubAppConfig(
                client_id="test-client-id",
                client_secret="test-client-secret",
                app_id="12345",
                private_key_path="/tmp/fake-key.pem",
                webhook_secret=WEBHOOK_SECRET,
                name="test-app",
            ),
        )
    )
    app._plugins.append(github_plugin)

    yield app

    Base.metadata.drop_all(bind=db.engine)
    app._plugins.remove(github_plugin)


@pytest.fixture
def github_client(github_app):
    """Test client with GitHub plugin enabled."""
    from fastapi.testclient import TestClient

    return TestClient(github_app.web.get_asgi_app())


def _post(client, event: str, payload: dict):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )


def _install(client, installation_id="123"):
    """Create the org the repo events attach to."""
    return _post(
        client,
        "installation",
        {
            "action": "created",
            "installation": {
                "id": installation_id,
                "account": {
                    "id": "456",
                    "login": "test-org",
                    "avatar_url": "https://github.com/org-avatar.png",
                },
            },
            "repositories": [],
            "sender": {"id": 100, "login": "admin-user"},
        },
    )


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_added_repo_gets_a_web_url_from_full_name(mock_broker, github_client, github_app):
    """The added-repos payload has no html_url, so the URL is derived."""
    mock_broker.return_value = AsyncMock()
    _install(github_client)

    response = _post(
        github_client,
        "installation_repositories",
        {
            "action": "added",
            "installation": {"id": "123", "account": {"id": "456", "login": "test-org"}},
            # Exactly what GitHub sends: no html_url, no owner.
            "repositories_added": [
                {
                    "id": 9001,
                    "node_id": "R_kg",
                    "name": "late-repo",
                    "full_name": "test-org/late-repo",
                    "private": False,
                }
            ],
            "sender": {"id": 100, "login": "admin-user"},
        },
    )

    assert response.status_code == 202
    with github_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9001")
        assert repo is not None
        assert repo.web_url == "https://github.com/test-org/late-repo"
        # Falls back to the org avatar rather than leaving the row blank.
        assert repo.avatar_url == "https://github.com/org-avatar.png"


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_added_repo_queues_pr_and_issue_backfill(mock_broker, github_client):
    """A repo added later may already have open PRs and issues."""
    broker = AsyncMock()
    mock_broker.return_value = broker
    _install(github_client)
    broker.publish.reset_mock()

    _post(
        github_client,
        "installation_repositories",
        {
            "action": "added",
            "installation": {"id": "123", "account": {"id": "456", "login": "test-org"}},
            "repositories_added": [
                {"id": 9002, "name": "late-repo", "full_name": "test-org/late-repo"}
            ],
            "sender": {"id": 100, "login": "admin-user"},
        },
    )

    published = broker.publish.call_args
    assert published.kwargs["stream"] == "jeanclode.events.github.sync_repositories"
    assert published.args[0].external_repo_ids == ["9002"]


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_repository_created_event_creates_repo(mock_broker, github_client, github_app):
    """An org-wide install announces new repos through ``repository`` alone."""
    broker = AsyncMock()
    mock_broker.return_value = broker
    _install(github_client)
    broker.publish.reset_mock()

    response = _post(
        github_client,
        "repository",
        {
            "action": "created",
            "repository": {
                "id": 9100,
                "name": "brand-new",
                "full_name": "test-org/brand-new",
                "html_url": "https://github.com/test-org/brand-new",
                "owner": {"login": "test-org", "avatar_url": "https://github.com/a.png"},
            },
            "installation": {"id": "123"},
            "sender": {"id": 100, "login": "admin-user"},
        },
    )

    assert response.status_code == 202
    assert response.json()["processed"] is True
    with github_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9100")
        assert repo is not None
        assert repo.web_url == "https://github.com/test-org/brand-new"
        assert repo.avatar_url == "https://github.com/a.png"

    assert broker.publish.call_args.kwargs["stream"] == "jeanclode.events.github.sync_repositories"


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_repository_renamed_updates_the_row(mock_broker, github_client, github_app):
    """A rename only ever arrives on the ``repository`` event."""
    broker = AsyncMock()
    mock_broker.return_value = broker
    _install(github_client)

    def _repo_event(action, name):
        return _post(
            github_client,
            "repository",
            {
                "action": action,
                "repository": {
                    "id": 9101,
                    "name": name,
                    "full_name": f"test-org/{name}",
                    "html_url": f"https://github.com/test-org/{name}",
                    "owner": {"login": "test-org"},
                },
                "installation": {"id": "123"},
                "sender": {"id": 100, "login": "admin-user"},
            },
        )

    _repo_event("created", "old-name")
    broker.publish.reset_mock()
    _repo_event("renamed", "new-name")

    with github_app.database.session() as db:
        repo = db_get_repository_by_external_id(db, "9101")
        assert repo is not None
        assert repo.name == "new-name"
        assert repo.web_url == "https://github.com/test-org/new-name"

    # Already tracked — a rename is not a reason to re-backfill.
    broker.publish.assert_not_called()


@patch("api.routers.webhooks.github.installations.get_faststream_broker")
def test_repository_deleted_removes_the_row(mock_broker, github_client, github_app):
    """A deleted repo stops being dispatchable."""
    mock_broker.return_value = AsyncMock()
    _install(github_client)
    _post(
        github_client,
        "repository",
        {
            "action": "created",
            "repository": {
                "id": 9102,
                "name": "doomed",
                "full_name": "test-org/doomed",
                "html_url": "https://github.com/test-org/doomed",
            },
            "installation": {"id": "123"},
            "sender": {"id": 100, "login": "admin-user"},
        },
    )

    response = _post(
        github_client,
        "repository",
        {
            "action": "deleted",
            "repository": {"id": 9102, "name": "doomed", "full_name": "test-org/doomed"},
            "installation": {"id": "123"},
            "sender": {"id": 100, "login": "admin-user"},
        },
    )

    assert response.status_code == 202
    with github_app.database.session() as db:
        assert db_get_repository_by_external_id(db, "9102") is None
