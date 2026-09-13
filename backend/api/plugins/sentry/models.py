"""Pydantic models for Sentry API responses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SentryOrganization(BaseModel):
    """A Sentry organization (from API)."""

    id: str
    slug: str
    name: str


class SentryProject(BaseModel):
    """A Sentry project."""

    id: str
    slug: str
    name: str
    platform: str | None = None
    date_created: datetime | None = None
    status: str | None = None


class SentryCodeMapping(BaseModel):
    """A code mapping linking a Sentry project to a repository."""

    id: str
    project_slug: str = Field(alias="projectSlug")
    repo_name: str = Field(alias="repoName")
    provider: dict | str | None = None
    stack_root: str = Field(default="", alias="stackRoot")
    source_root: str = Field(default="", alias="sourceRoot")
    default_branch: str | None = Field(default=None, alias="defaultBranch")


class SentryIssue(BaseModel):
    """A Sentry issue (group of events)."""

    id: str
    title: str
    culprit: str | None = None
    level: str = "error"
    status: str | None = None
    first_seen: datetime | None = Field(default=None, alias="firstSeen")
    last_seen: datetime | None = Field(default=None, alias="lastSeen")
    count: str | None = None
    project: SentryProject | None = None
    short_id: str | None = Field(default=None, alias="shortId")
    permalink: str | None = None
    metadata: dict | None = None
    type: str | None = None


class SentryStacktraceFrame(BaseModel):
    """A single frame in a stacktrace."""

    filename: str | None = None
    function: str | None = None
    module: str | None = None
    lineno: int | None = None
    colno: int | None = None
    abs_path: str | None = Field(default=None, alias="absPath")
    context: list[list[int | str]] | None = None
    in_app: bool | None = Field(default=None, alias="inApp")
    pre_context: list[str] | None = Field(default=None, alias="preContext")
    post_context: list[str] | None = Field(default=None, alias="postContext")
    context_line: str | None = Field(default=None, alias="contextLine")


class SentryStacktrace(BaseModel):
    """A stacktrace from a Sentry event."""

    frames: list[SentryStacktraceFrame] = Field(default_factory=list)


class SentryExceptionValue(BaseModel):
    """A single exception value in an event."""

    type: str | None = None
    value: str | None = None
    module: str | None = None
    stacktrace: SentryStacktrace | None = None


class SentryExceptionEntry(BaseModel):
    """Exception entry containing exception values."""

    values: list[SentryExceptionValue] = Field(default_factory=list)


class SentryBreadcrumb(BaseModel):
    """A breadcrumb from a Sentry event."""

    timestamp: datetime | None = None
    category: str | None = None
    message: str | None = None
    level: str | None = None
    type: str | None = None
    data: dict[str, object] | None = None


class SentryEventContext(BaseModel):
    """Context data from a Sentry event."""

    os: dict[str, object] | None = None
    browser: dict[str, object] | None = None
    runtime: dict[str, object] | None = None
    device: dict[str, object] | None = None


class SentryEvent(BaseModel):
    """A Sentry event (single occurrence)."""

    event_id: str = Field(alias="eventID")
    id: str | None = None
    title: str | None = None
    message: str | None = None
    platform: str | None = None
    date_created: datetime | None = Field(default=None, alias="dateCreated")
    tags: list[dict[str, str]] | None = None
    entries: list[dict[str, object]] | None = None
    contexts: SentryEventContext | None = None
    context: dict[str, object] | None = None
    sdk: dict[str, object] | None = None

    def get_exceptions(self) -> list[SentryExceptionValue]:
        """Extract exception values from event entries."""
        exceptions: list[SentryExceptionValue] = []
        for entry in self.entries or []:
            if entry.get("type") == "exception":
                data = entry.get("data", {})
                if isinstance(data, dict):
                    parsed = SentryExceptionEntry.model_validate(data)
                    exceptions.extend(parsed.values)
        return exceptions

    def get_breadcrumbs(self) -> list[SentryBreadcrumb]:
        """Extract breadcrumbs from event entries."""
        breadcrumbs: list[SentryBreadcrumb] = []
        for entry in self.entries or []:
            if entry.get("type") == "breadcrumbs":
                data = entry.get("data", {})
                if isinstance(data, dict):
                    for crumb in data.get("values", []):
                        if isinstance(crumb, dict):
                            breadcrumbs.append(SentryBreadcrumb.model_validate(crumb))
        return breadcrumbs


class SentryRelease(BaseModel):
    """A Sentry release."""

    version: str
    date_created: datetime | None = Field(default=None, alias="dateCreated")
    date_released: datetime | None = Field(default=None, alias="dateReleased")
    short_version: str | None = Field(default=None, alias="shortVersion")
    new_groups: int | None = Field(default=None, alias="newGroups")


class SentryWebhookSubscription(BaseModel):
    """A Sentry webhook subscription."""

    id: str
    url: str | None = None
    events: list[str] = Field(default_factory=list)
    status: str | None = None
