"""Integration settings schemas.

Pydantic models that define the shape of the JSONB `settings` column
on Organization and Repository. The DB stores raw JSON;
these models validate and fill defaults when reading.
"""

import uuid
from enum import StrEnum
from typing import Annotated, Any, Union

from pydantic import BaseModel, Discriminator, Field, Tag

# ---------------------------------------------------------------------------
# Sentry trigger enums
# ---------------------------------------------------------------------------


class TriageTrigger(StrEnum):
    """When to triage incoming Sentry issues."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"


class BackfillScope(StrEnum):
    """How much existing Sentry history to import when an org is connected.

    Backfill runs once per connect (and on demand afterwards).  A large org
    can carry thousands of unresolved issues, so this bounds how many enter
    the pending pool at onboarding time rather than forcing a choice between
    a flood and an empty dashboard.
    """

    NONE = "none"
    DAYS_7 = "7d"
    DAYS_30 = "30d"
    ALL = "all"


# Lookback window per scope, in days. ``None`` means no cutoff (import all).
BACKFILL_LOOKBACK_DAYS: dict[BackfillScope, int | None] = {
    BackfillScope.DAYS_7: 7,
    BackfillScope.DAYS_30: 30,
    BackfillScope.ALL: None,
}


class BatchWindow(StrEnum):
    """How long the dispatcher waits between batches for a Sentry org.

    A longer window lets more related errors accumulate into one PR (ADR-002's
    cascade/dedup benefit) at the cost of dispatch latency.
    """

    MINUTES_5 = "5m"
    HOUR_1 = "1h"
    HOURS_12 = "12h"
    DAY_1 = "1d"
    DAYS_3 = "3d"
    WEEK_1 = "1w"


# Dispatch window per setting, in minutes. Kept alongside the enum so the SQL
# CASE built from it (``batch_window_minutes_clause``) and the Pydantic
# default can't drift apart.
BATCH_WINDOW_MINUTES: dict[BatchWindow, int] = {
    BatchWindow.MINUTES_5: 5,
    BatchWindow.HOUR_1: 60,
    BatchWindow.HOURS_12: 720,
    BatchWindow.DAY_1: 1440,
    BatchWindow.DAYS_3: 4320,
    BatchWindow.WEEK_1: 10080,
}


def batch_window_minutes(value: str | None) -> int:
    """Resolve a stored ``batch_window`` string to its minute count.

    Unknown/absent values fall back to the 5-minute default, matching
    ``batch_window_minutes_clause`` and the Pydantic default.
    """
    try:
        return BATCH_WINDOW_MINUTES[BatchWindow(value or "")]
    except ValueError:
        return BATCH_WINDOW_MINUTES[BatchWindow.MINUTES_5]


class BatchSize(StrEnum):
    """How many Sentry issues one dispatch batch may carry for an org.

    Mirrors the ``BatchWindow`` / ``BATCH_WINDOW_MINUTES`` pair: the enum is
    the stored value, ``BATCH_SIZE_VALUES`` the integer it resolves to.
    """

    ONE = "1"
    THREE = "3"
    FIVE = "5"
    TEN = "10"


BATCH_SIZE_VALUES: dict[BatchSize, int] = {
    BatchSize.ONE: 1,
    BatchSize.THREE: 3,
    BatchSize.FIVE: 5,
    BatchSize.TEN: 10,
}


def batch_size_value(value: str | None, default: int) -> int:
    """Resolve a stored ``batch_size`` string to its issue count.

    Unknown/absent values fall back to ``default`` — the dispatcher's own
    config-level batch size, which is what applied before the setting existed.
    """
    try:
        return BATCH_SIZE_VALUES[BatchSize(value or "")]
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------


class NotifySettings(BaseModel):
    """Who to @-mention when Jeanclode is done with a PR/MR it opened.

    Provider identity ids, not handles: a GitLab/GitHub rename would
    otherwise leave a stale username here and silently stop notifying.
    Resolved to handles at dispatch time (``add_notify_to_inputs``).
    """

    on_ready: list[uuid.UUID] = Field(
        default_factory=list,
        description="Provider identity IDs to mention once a bot-opened PR/MR is ready",
    )


class RelatedRepoSettings(BaseModel):
    """Which repos join a run's workspace alongside the one it was triggered on.

    Only ``issue-resolve`` and ``sentry-fix`` clone more than one repo;
    review, summary and respond stay single-repo by design.

    ``always_include`` is deliberately one-directional and is *not* a
    ``RepositoryMapping``: a run on any repo under this org pulls the listed
    repos in, but a run on one of the listed repos pulls nothing extra.
    Mappings are symmetric (``db_get_related_repos`` reads both columns), so
    a row there would drag the whole group along in both directions.
    """

    always_include: list[uuid.UUID] = Field(
        default_factory=list,
        description="Repository IDs cloned into every multi-repo run under this org",
    )
    pack_subgroup: bool = Field(
        default=True,
        description="Also clone the other repos sitting directly in the run's own subgroup",
    )
    excluded_subgroups: list[uuid.UUID] = Field(
        default_factory=list,
        description="Subgroup org IDs that never get packed",
    )


class TriggerPermission(StrEnum):
    """Who is allowed to trigger ``respond`` with an ``@jeanclode-bot`` mention.

    ``DEVELOPER_ONLY`` is the default and matches GitHub/GitLab's own
    "must have write access to trigger CI from a comment" convention:
    the commenter needs collaborator write+ (GitHub) or Developer+
    (GitLab). ``ANYONE`` skips that check entirely — anyone who can
    comment on the repo (including outside contributors on a public
    repo) can invoke the bot.
    """

    ANYONE = "anyone"
    DEVELOPER_ONLY = "developer_only"


class GitOrgSettings(BaseModel):
    """Settings stored on an Organization with provider=github/gitlab (JSONB).

    Review, summary and issue-resolve dispatch are always label-driven
    (``jeanclode:review`` / ``jeanclode:summary`` / ``jeanclode:resolve``,
    see :mod:`api.plugins.github.dispatch`) and respond always fires on an
    ``@jeanclode-bot`` mention — neither is configurable per org. What *is*
    configurable is who a mention listens to at all (``trigger_permission``).
    """

    notify: NotifySettings = Field(default_factory=NotifySettings)
    # GitLab only. On GitLab Free, group webhooks are Premium-only, so the one
    # group token can't carry a webhook. Turning this on makes Jeanclode create
    # its webhook on every project the org owns. New projects still need an
    # instance system hook (without merge request events) to be discovered —
    # see backend/CLAUDE.md. No-op for GitHub orgs.
    manage_project_webhooks: bool = Field(default=False)
    related_repos: RelatedRepoSettings = Field(default_factory=RelatedRepoSettings)
    trigger_permission: TriggerPermission = Field(
        default=TriggerPermission.DEVELOPER_ONLY,
        description="Who an @jeanclode-bot mention listens to: anyone, or collaborators with write+ access",
    )


class RepoSettings(BaseModel):
    """Per-repository settings. Inherits org triggers when enabled."""

    enabled: bool = Field(default=True, description="Whether triggers are active for this repo")


class SentryTriggerSettings(BaseModel):
    """Trigger configuration for Sentry workflows."""

    triage: TriageTrigger = Field(default=TriageTrigger.MANUAL)


class SentryOrgSettings(BaseModel):
    """Settings stored on an Organization with provider=sentry (JSONB)."""

    triggers: SentryTriggerSettings = Field(default_factory=SentryTriggerSettings)
    backfill: BackfillScope = Field(
        default=BackfillScope.NONE,
        description="How much existing Sentry history to import on connect",
    )
    batch_window: BatchWindow = Field(
        default=BatchWindow.MINUTES_5,
        description="Minimum time between dispatch batches for this org",
    )
    batch_size: BatchSize = Field(
        default=BatchSize.FIVE,
        description="Max Sentry issues carried by a single dispatch batch",
    )
    gate_on_open_fix_prs: bool = Field(
        default=False,
        description=(
            "Don't start the next batch until every PR the previous batch "
            "opened has been merged or closed. Off by default."
        ),
    )


# ---------------------------------------------------------------------------
# Settings resolver
# ---------------------------------------------------------------------------

_SETTINGS_BY_PROVIDER: dict[str, type[BaseModel]] = {
    "github": GitOrgSettings,
    "gitlab": GitOrgSettings,
    "sentry": SentryOrgSettings,
}


def get_settings_model(provider: str) -> type[BaseModel]:
    """Return the Pydantic settings class for a given provider."""
    return _SETTINGS_BY_PROVIDER[provider]


def merge_settings(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``incoming`` into ``current``, returning a new dict.

    Nested dicts merge key by key; every other value overwrites. Used both to
    keep a partial ``PATCH`` (say, only ``triggers.triage``) from wiping
    sibling settings the caller never mentioned, and to layer a GitLab
    group's settings down onto its subgroups.
    """
    merged: dict[str, Any] = dict(current)
    for key, value in incoming.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Discriminated union for API endpoints
# ---------------------------------------------------------------------------


def _settings_discriminator(v: dict | BaseModel) -> str:
    """Discriminate by the presence of provider-specific trigger fields."""
    if isinstance(v, SentryOrgSettings):
        return "sentry"
    if isinstance(v, GitOrgSettings):
        return "git"
    # Dict fallback — sentry payloads carry triggers.triage, a backfill
    # scope, a batch window/size, or the open-fix-PR gate; git payloads
    # carry no `triggers` key at all. A partial PATCH may omit triggers
    # entirely, so any of those fields alone identifies a sentry payload.
    sentry_keys = ("backfill", "batch_window", "batch_size", "gate_on_open_fix_prs")
    if isinstance(v, dict) and any(k in v for k in sentry_keys):
        return "sentry"
    triggers = v.get("triggers", {}) if isinstance(v, dict) else {}
    if "triage" in triggers:
        return "sentry"
    return "git"


OrgSettings = Annotated[
    Union[
        Annotated[GitOrgSettings, Tag("git")],
        Annotated[SentryOrgSettings, Tag("sentry")],
    ],
    Discriminator(_settings_discriminator),
]
# Union type for organization settings — discriminated by trigger shape.
