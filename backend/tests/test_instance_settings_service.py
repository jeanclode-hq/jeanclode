"""Tests for the encrypted instance-settings service layer."""

import threading

from api.database.instance_settings import (
    db_get_setting,
    db_get_settings_by_category,
)
from api.services.instance_settings import (
    delete_category,
    get_or_create_memory_signing_secret,
    has_any_git_provider,
    load_github_config,
    load_gitlab_config,
    save_github_config,
    save_gitlab_config,
)


def test_save_and_load_github_round_trip(app, db_session):
    save_github_config(
        db_session,
        {
            "client_id": "Iv1.abc",
            "client_secret": "super-secret",
            "app_id": "12345",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----",
            "webhook_secret": "hooky",
            "name": "jeanclode-test",
        },
    )

    loaded = load_github_config(db_session)
    assert loaded is not None
    assert loaded["client_id"] == "Iv1.abc"
    assert loaded["client_secret"] == "super-secret"
    assert loaded["app_id"] == "12345"
    assert loaded["private_key_pem"].startswith("-----BEGIN PRIVATE KEY-----")
    assert loaded["webhook_secret"] == "hooky"
    assert loaded["name"] == "jeanclode-test"


def test_stored_values_are_encrypted_at_rest(app, db_session):
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

    row = db_get_setting(db_session, "github", "client_secret")
    assert row is not None
    # Encrypted value should never contain the plaintext.
    assert "super-secret" not in row.value_encrypted


def test_load_returns_none_when_nothing_saved(app, db_session):
    assert load_github_config(db_session) is None
    assert load_gitlab_config(db_session) is None


def test_save_gitlab_round_trip(app, db_session):
    save_gitlab_config(
        db_session,
        {
            "client_id": "gl-client",
            "client_secret": "gl-secret",
            "instance_url": "https://gitlab.example.com",
        },
    )

    gl = load_gitlab_config(db_session)
    assert gl is not None
    assert gl["client_id"] == "gl-client"
    assert gl["instance_url"] == "https://gitlab.example.com"


def test_get_or_create_memory_signing_secret_generates_and_persists(app, db_session):
    secret = get_or_create_memory_signing_secret(db_session)
    assert secret

    row = db_get_setting(db_session, "memory", "internal_signing_secret")
    assert row is not None
    assert secret not in row.value_encrypted


def test_get_or_create_memory_signing_secret_is_stable_across_calls(app, db_session):
    first = get_or_create_memory_signing_secret(db_session)
    second = get_or_create_memory_signing_secret(db_session)
    assert first == second


def test_get_or_create_memory_signing_secret_concurrent_first_start_converges(app, db_session):
    """Two backend replicas racing to generate the secret on first start must
    converge on the same value — never mint tokens with different secrets."""
    db_plugin = app.database
    assert db_plugin is not None

    results: list[str] = []

    def _attempt() -> None:
        with db_plugin.session() as db:
            results.append(get_or_create_memory_signing_secret(db))

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert results[0] == results[1]


def test_delete_category_removes_rows(app, db_session):
    save_github_config(
        db_session,
        {
            "client_id": "a",
            "client_secret": "b",
            "app_id": "1",
            "private_key_pem": "pem",
            "webhook_secret": "h",
            "name": "n",
        },
    )
    assert load_github_config(db_session) is not None

    removed = delete_category(db_session, "github")
    assert removed >= 1
    assert db_get_settings_by_category(db_session, "github") == []
    assert load_github_config(db_session) is None


def test_has_any_git_provider_tracks_state(app, db_session):
    assert has_any_git_provider(db_session) is False

    save_gitlab_config(
        db_session,
        {
            "client_id": "a",
            "client_secret": "b",
            "instance_url": "https://gitlab.com",
        },
    )
    assert has_any_git_provider(db_session) is True


def test_upsert_overwrites_existing(app, db_session):
    save_github_config(
        db_session,
        {
            "client_id": "old",
            "client_secret": "old-secret",
            "app_id": "1",
            "private_key_pem": "pem",
            "webhook_secret": "h",
            "name": "n",
        },
    )
    save_github_config(
        db_session,
        {
            "client_id": "new",
            "client_secret": "new-secret",
            "app_id": "2",
            "private_key_pem": "pem2",
            "webhook_secret": "h2",
            "name": "n2",
        },
    )

    loaded = load_github_config(db_session)
    assert loaded is not None
    assert loaded["client_id"] == "new"
    assert loaded["client_secret"] == "new-secret"
    assert loaded["app_id"] == "2"
