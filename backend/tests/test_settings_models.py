"""Tests for the integration settings Pydantic models."""

from __future__ import annotations

from api.models.settings import GitOrgSettings, TriggerPermission


def test_trigger_permission_defaults_to_developer_only():
    """An org that never touched the setting keeps today's behavior: a
    mention only listens to collaborators with write+ access."""
    assert GitOrgSettings().trigger_permission == TriggerPermission.DEVELOPER_ONLY


def test_trigger_permission_parses_anyone():
    settings = GitOrgSettings.model_validate({"trigger_permission": "anyone"})
    assert settings.trigger_permission == TriggerPermission.ANYONE


def test_manage_project_webhooks_defaults_off():
    assert GitOrgSettings().manage_project_webhooks is False
    assert (
        GitOrgSettings.model_validate({"manage_project_webhooks": True}).manage_project_webhooks
        is True
    )


def test_notify_defaults_to_nobody():
    """Notifications are opt-in: an org that never touched the setting must
    not start @-mentioning anyone."""
    assert GitOrgSettings().notify.on_ready == []


def test_notify_fills_from_stored_json_without_the_key():
    """Orgs persisted before the field existed must still parse."""
    settings = GitOrgSettings.model_validate({"manage_project_webhooks": True})
    assert settings.notify.on_ready == []


def test_notify_ignores_stale_trigger_settings():
    """Orgs persisted before triggers were removed must still parse — the
    stale ``triggers`` key is simply dropped."""
    settings = GitOrgSettings.model_validate({"triggers": {"review": "manual"}})
    assert settings.notify.on_ready == []


def test_notify_parses_identity_ids():
    import uuid

    ident = uuid.uuid4()
    settings = GitOrgSettings.model_validate({"notify": {"on_ready": [str(ident)]}})
    assert settings.notify.on_ready == [ident]


def test_notify_only_payload_still_discriminates_as_git():
    """A PATCH carrying nothing but the notify list must not be mistaken for
    a sentry settings payload by the discriminated union."""
    from pydantic import TypeAdapter

    from api.models.settings import OrgSettings

    parsed = TypeAdapter(OrgSettings).validate_python({"notify": {"on_ready": []}})
    assert isinstance(parsed, GitOrgSettings)


def test_sentry_batch_settings_defaults_and_parse():
    from api.models.settings import BatchSize, BatchWindow, SentryOrgSettings

    s = SentryOrgSettings()
    assert s.batch_size == BatchSize.FIVE
    assert s.gate_on_open_fix_prs is False

    parsed = SentryOrgSettings.model_validate(
        {"batch_size": "10", "batch_window": "1w", "gate_on_open_fix_prs": True}
    )
    assert parsed.batch_size == BatchSize.TEN
    assert parsed.batch_window == BatchWindow.WEEK_1
    assert parsed.gate_on_open_fix_prs is True


def test_batch_size_only_payload_discriminates_as_sentry():
    """A PATCH carrying only ``batch_size`` must route to the sentry model."""
    from pydantic import TypeAdapter

    from api.models.settings import OrgSettings, SentryOrgSettings

    parsed = TypeAdapter(OrgSettings).validate_python({"batch_size": "3"})
    assert isinstance(parsed, SentryOrgSettings)

    gated = TypeAdapter(OrgSettings).validate_python({"gate_on_open_fix_prs": True})
    assert isinstance(gated, SentryOrgSettings)


def test_batch_window_and_size_minute_helpers():
    from api.models.settings import batch_size_value, batch_window_minutes

    assert batch_window_minutes("3d") == 4320
    assert batch_window_minutes("bogus") == 5
    assert batch_size_value("1", default=5) == 1
    assert batch_size_value(None, default=7) == 7


def test_notify_merge_does_not_clobber_sibling_settings():
    """``merge_settings`` is what keeps a partial PATCH from resetting the
    siblings the caller never mentioned."""
    from api.models.settings import merge_settings

    stored = {"manage_project_webhooks": True, "notify": {"on_ready": ["a"]}}
    merged = merge_settings(stored, {"notify": {"on_ready": ["b"]}})
    assert merged["manage_project_webhooks"] is True
    assert merged["notify"]["on_ready"] == ["b"]
