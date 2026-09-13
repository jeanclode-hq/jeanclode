"""Lightweight Sentry API client — only fetches what the CLI needs for pre-flight.

The full issue/event data fetching is handled by the plugin's fetch-sentry-data.py.
This client only resolves repo URLs from code mappings.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def _auth_headers(token: str | None) -> dict[str, str]:
    """Build request headers. In sandboxed mode the agent runs token-less and
    the security-proxy injects auth on the wire — so we omit Authorization
    when no token is set rather than sending an empty Bearer."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_code_mappings(api_url: str, org_slug: str, token: str | None) -> list[dict]:
    """Fetch code mappings for an organization."""
    url = f"{api_url}/api/0/organizations/{org_slug}/code-mappings/"
    req = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        logger.debug("Failed to fetch code mappings for %s", org_slug, exc_info=True)
        return []


def fetch_issue_project_slug(api_url: str, issue_id: str, token: str | None) -> str:
    """Fetch the project slug for an issue (needed to match code mappings)."""
    url = f"{api_url}/api/0/issues/{issue_id}/"
    req = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("project", {}).get("slug", "")
    except Exception:
        logger.debug("Failed to fetch issue %s", issue_id, exc_info=True)
        return ""


def resolve_api_url(sentry_url: str, token: str | None) -> str:
    """Auto-discover the Sentry region API URL from an issue URL.

    Sentry routes to region-specific API endpoints. If the user hasn't
    set SENTRY_API_URL explicitly, we discover it from the org slug.
    """
    import os
    import re

    configured = os.environ.get("SENTRY_API_URL", "").rstrip("/")
    if configured and configured != "https://sentry.io":
        return configured

    match = re.match(r"https://([^.]+)\.sentry\.io/", sentry_url)
    if not match:
        return configured or "https://sentry.io"
    org_slug = match.group(1)

    url = f"https://sentry.io/api/0/organizations/{org_slug}/"
    req = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            region_url = data.get("links", {}).get("regionUrl")
            if region_url:
                logger.info("Discovered Sentry region: %s", region_url)
                return region_url.rstrip("/")
    except Exception:
        logger.debug("Region discovery failed for org %s", org_slug, exc_info=True)

    return configured or "https://sentry.io"
