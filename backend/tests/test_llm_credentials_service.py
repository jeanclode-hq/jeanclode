"""Tests for the LLM credential pool service + selection logic (ADR-010)."""

from datetime import UTC, datetime, timedelta

from api.database.llm_credentials import (
    LLMCredentialAvailability,
    db_mark_llm_credential_stale,
    db_select_llm_credential,
)
from api.models.llm_credentials import LLMCredentialStatus
from api.services.llm_credentials import (
    create_llm_credential,
    decrypt_secret,
    delete_llm_credential,
    list_llm_credentials,
    reorder_llm_credentials,
    stale_until_from_retry_after,
    update_llm_credential,
)


def _make(db_session, priority_hint: str = "a", **overrides):
    defaults = {
        "kind": "api_key",
        "provider": "anthropic",
        "secret": f"sk-ant-{priority_hint}",
        "model_high": "claude-opus-4-6",
        "model_low": "claude-haiku-4-5",
    }
    defaults.update(overrides)
    return create_llm_credential(db_session, **defaults)


def test_create_assigns_incrementing_priority(app, db_session):
    first = _make(db_session, "a")
    second = _make(db_session, "b")
    assert first.priority == 1
    assert second.priority == 2


def test_secret_is_encrypted_at_rest_and_round_trips(app, db_session):
    credential = _make(db_session, secret="super-secret-token")
    assert "super-secret-token" not in credential.secret_encrypted
    assert decrypt_secret(credential) == "super-secret-token"


def test_list_llm_credentials_ordered_by_priority(app, db_session):
    _make(db_session, "a")
    _make(db_session, "b")
    _make(db_session, "c")

    listed = list_llm_credentials(db_session)
    assert [c.priority for c in listed] == [1, 2, 3]


def test_update_without_secret_keeps_existing_encrypted_value(app, db_session):
    credential = _make(db_session, secret="original")
    updated = update_llm_credential(db_session, credential.id, model_high="claude-sonnet-4-6")
    assert updated is not None
    assert updated.model_high == "claude-sonnet-4-6"
    assert decrypt_secret(updated) == "original"


def test_update_with_secret_rotates_it(app, db_session):
    credential = _make(db_session, secret="original")
    updated = update_llm_credential(db_session, credential.id, secret="rotated")
    assert updated is not None
    assert decrypt_secret(updated) == "rotated"


def test_delete_llm_credential_removes_row(app, db_session):
    credential = _make(db_session)
    assert delete_llm_credential(db_session, credential.id) is True
    assert list_llm_credentials(db_session) == []


def test_reorder_reassigns_priority_in_given_order(app, db_session):
    first = _make(db_session, "a")
    second = _make(db_session, "b")
    third = _make(db_session, "c")

    reordered = reorder_llm_credentials(db_session, [third.id, first.id, second.id])
    by_id = {c.id: c.priority for c in reordered}
    assert by_id[third.id] == 1
    assert by_id[first.id] == 2
    assert by_id[second.id] == 3


def test_select_credential_none_configured_on_empty_pool(app, db_session):
    availability, credential, retry_at = db_select_llm_credential(db_session)
    assert availability == LLMCredentialAvailability.NONE_CONFIGURED
    assert credential is None
    assert retry_at is None


def test_select_credential_picks_first_priority_row(app, db_session):
    first = _make(db_session, "a")
    _make(db_session, "b")

    availability, credential, retry_at = db_select_llm_credential(db_session)
    assert availability == LLMCredentialAvailability.AVAILABLE
    assert credential is not None
    assert credential.id == first.id
    assert retry_at is None


def test_select_credential_skips_stale_row_for_next_priority(app, db_session):
    first = _make(db_session, "a")
    second = _make(db_session, "b")
    db_mark_llm_credential_stale(
        db_session, first.id, stale_until=datetime.now(UTC) + timedelta(hours=1)
    )

    availability, credential, retry_at = db_select_llm_credential(db_session)
    assert availability == LLMCredentialAvailability.AVAILABLE
    assert credential is not None
    assert credential.id == second.id
    assert retry_at is None


def test_select_credential_all_stale_returns_earliest_retry_at(app, db_session):
    first = _make(db_session, "a")
    second = _make(db_session, "b")
    now = datetime.now(UTC)
    later = now + timedelta(hours=2)
    sooner = now + timedelta(minutes=30)
    db_mark_llm_credential_stale(db_session, first.id, stale_until=later)
    db_mark_llm_credential_stale(db_session, second.id, stale_until=sooner)

    availability, credential, retry_at = db_select_llm_credential(db_session)
    assert availability == LLMCredentialAvailability.ALL_STALE
    assert credential is None
    assert retry_at == sooner


def test_select_credential_treats_elapsed_stale_until_as_available(app, db_session):
    credential = _make(db_session)
    db_mark_llm_credential_stale(
        db_session, credential.id, stale_until=datetime.now(UTC) - timedelta(hours=1)
    )

    availability, selected, _retry_at = db_select_llm_credential(db_session)
    assert availability == LLMCredentialAvailability.AVAILABLE
    assert selected is not None
    assert selected.id == credential.id


def test_mark_stale_is_idempotent_and_overwritable(app, db_session):
    credential = _make(db_session)
    first_stale_until = datetime.now(UTC) + timedelta(hours=5)
    db_mark_llm_credential_stale(db_session, credential.id, stale_until=first_stale_until)

    reloaded = list_llm_credentials(db_session)[0]
    assert reloaded.status == LLMCredentialStatus.STALE.value

    corrected_stale_until = datetime.now(UTC) + timedelta(minutes=10)
    db_mark_llm_credential_stale(db_session, credential.id, stale_until=corrected_stale_until)
    reloaded = list_llm_credentials(db_session)[0]
    assert reloaded.stale_until is not None
    assert abs((reloaded.stale_until - corrected_stale_until).total_seconds()) < 1


def test_stale_until_from_retry_after_uses_five_hour_default():
    now = datetime.now(UTC)
    result = stale_until_from_retry_after(None)
    assert timedelta(hours=4, minutes=59) < (result - now) < timedelta(hours=5, minutes=1)


def test_stale_until_from_retry_after_uses_provided_seconds():
    now = datetime.now(UTC)
    result = stale_until_from_retry_after(120)
    assert timedelta(minutes=1, seconds=59) < (result - now) < timedelta(minutes=2, seconds=1)
