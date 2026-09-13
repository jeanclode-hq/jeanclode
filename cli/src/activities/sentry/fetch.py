"""Fetch Sentry issue data and format it for the triage agent."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

from src.activities.decorator import activity
from src.activities.sentry.schemas import SentryEvent
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


def _extract_issue_id(value: str) -> str:
    if re.fullmatch(r"\d+", value):
        return value
    match = re.search(r"/issues/(\d+)", value)
    return match.group(1) if match else ""


def _extract_org_slug(url: str) -> str | None:
    match = re.match(r"https://([^.]+)\.sentry\.io/", url)
    return match.group(1) if match else None


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_get(api_url: str, path: str, token: str | None) -> dict:
    url = f"{api_url}/api/0/{path}"
    req = urllib.request.Request(url, headers=_auth_headers(token))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _format_event(issue: dict, event: dict, repo_url: str | None, commit_sha: str | None) -> str:
    parts: list[str] = ["## Pre-fetched Sentry Data\n"]
    parts.append(f"### Issue #{issue.get('id', '')}")
    parts.append(f"- **Title:** {issue.get('title', '')}")
    parts.append(f"- **Status:** {issue.get('status', '')}")
    parts.append(f"- **Level:** {issue.get('level', '')}")
    parts.append(f"- **Count:** {issue.get('count', 0)}")
    parts.append(f"- **Culprit:** {issue.get('culprit', '')}")
    if issue.get("permalink"):
        parts.append(f"- **Permalink:** {issue['permalink']}")
    project = issue.get("project", {})
    if project:
        parts.append(f"- **Project:** {project.get('slug', '')}")
    metadata = issue.get("metadata", {})
    if metadata:
        parts.append(f"- **Metadata:** {metadata}")

    parts.append(f"\n### Latest Event ({event.get('eventID', '')})")
    parts.append(f"- **Title:** {event.get('title', '')}")

    release = event.get("release")
    if release:
        parts.append(f"- **Release:** {release.get('version', '')}")

    user = event.get("user")
    if user:
        parts.append(f"- **User:** {user}")

    tags = event.get("tags", [])
    if tags:
        parts.append("\n### Tags")
        for tag in tags:
            parts.append(f"- {tag.get('key', '')}: {tag.get('value', '')}")

    for entry in event.get("entries", []):
        parts.extend(_format_entry(entry))

    if repo_url:
        parts.append(f"\n### Repository: {repo_url}")
    if commit_sha:
        parts.append(f"### Commit: {commit_sha}")

    return "\n".join(parts)


def _format_entry(entry: dict[str, Any]) -> list[str]:
    entry_type = entry.get("type", "")
    entry_data = entry.get("data", {})
    out: list[str] = []
    if entry_type == "exception":
        out.append("\n### Exception")
        for exc_val in entry_data.get("values", []):
            out.append(f"- **Type:** {exc_val.get('type', '')}")
            out.append(f"- **Value:** {exc_val.get('value', '')}")
            st = exc_val.get("stacktrace", {})
            if st and st.get("frames"):
                out.append("\n#### Stacktrace (most recent last)")
                out.extend(_format_frames(st["frames"]))
    elif entry_type == "message":
        formatted = entry_data.get("formatted", "")
        if formatted:
            out.append("\n### Message")
            out.append(formatted)
    elif entry_type == "threads":
        for thread in entry_data.get("values", []):
            if thread.get("crashed") or thread.get("current"):
                st = thread.get("stacktrace", {})
                if st and st.get("frames"):
                    name = thread.get("name", f"Thread {thread.get('id', '?')}")
                    out.append(f"\n#### {name}")
                    out.extend(_format_frames(st["frames"]))
    return out


def _format_frames(frames: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for frame in frames:
        in_app = " [in_app]" if frame.get("in_app") else ""
        fn = frame.get("filename", "?")
        lineno = frame.get("lineno", "?")
        func = frame.get("function", "?")
        rows.append(f"  {fn}:{lineno} in {func}{in_app}")
    return rows


def _resolve_api_url_for(issue_url: str, token: str | None) -> str:
    configured = os.environ.get("SENTRY_API_URL", "").rstrip("/")
    if configured and configured != "https://sentry.io":
        return configured
    org_slug = _extract_org_slug(issue_url)
    if not org_slug:
        return configured or "https://sentry.io"
    try:
        url = f"https://sentry.io/api/0/organizations/{org_slug}/"
        req = urllib.request.Request(url, headers=_auth_headers(token))
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            region_url = data.get("links", {}).get("regionUrl")
            if region_url:
                return region_url.rstrip("/")
    except Exception:
        logger.debug("region discovery failed for %s", issue_url, exc_info=True)
    return configured or "https://sentry.io"


def _resolve_repo(api_url: str, issue: dict, org_slug: str | None, token: str | None) -> str | None:
    if not org_slug:
        return None
    try:
        mappings = _api_get(api_url, f"organizations/{org_slug}/code-mappings/", token)
    except Exception:
        return None
    project_slug = issue.get("project", {}).get("slug", "")
    for m in mappings:
        if m.get("projectSlug") == project_slug and m.get("repoName"):
            repo_name = m["repoName"]
            return repo_name if repo_name.startswith("http") else f"https://github.com/{repo_name}"
    return None


def _fetch_one(issue_url: str, token: str | None) -> SentryEvent | None:
    issue_id = _extract_issue_id(issue_url)
    if not issue_id:
        logger.warning("could not extract issue id from %s", issue_url)
        return None

    api_url = _resolve_api_url_for(issue_url, token)
    try:
        issue = _api_get(api_url, f"issues/{issue_id}/", token)
        event = _api_get(api_url, f"issues/{issue_id}/events/latest/", token)
    except Exception:
        logger.warning("failed to fetch sentry data for %s", issue_url, exc_info=True)
        return None

    org_slug = _extract_org_slug(issue_url)
    repo_url = _resolve_repo(api_url, issue, org_slug, token)

    commit_sha: str | None = None
    release = event.get("release")
    if release:
        version = release.get("version", "")
        if re.fullmatch(r"[0-9a-f]{7,40}", version):
            commit_sha = version

    return SentryEvent(
        issue_id=issue_id,
        sentry_url=issue_url,
        formatted=_format_event(issue, event, repo_url, commit_sha),
        repo_url=repo_url,
        commit_sha=commit_sha,
    )


@activity(name="Fetching Sentry issue")
def fetch_sentry_data(issue_urls: list[str], *, ctx: RunContext) -> list[SentryEvent]:
    """Fetch one or more Sentry issues and return formatted events.

    The token is read from ``ctx.env`` (then ``$SENTRY_AUTH_TOKEN`` as a
    fallback) so tests can drop one in via the RunContext directly.
    """
    token = ctx.env.get("SENTRY_AUTH_TOKEN") or os.environ.get("SENTRY_AUTH_TOKEN")
    events: list[SentryEvent] = []
    for url in issue_urls:
        event = _fetch_one(url, token)
        if event is not None:
            events.append(event)
    return events
