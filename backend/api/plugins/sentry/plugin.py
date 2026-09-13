"""Sentry plugin - Sentry API client built on BaseHttpPlugin."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from api.context import get_current_app
from api.plugins.container.backend import ContainerBackend
from api.plugins.container.docker import DockerBackend
from api.plugins.container.kubernetes import KubernetesBackend
from api.plugins.httpx.plugin import BaseHttpPlugin
from api.plugins.sentry.config import SentryPluginConfig
from api.plugins.sentry.dispatch import SentryDispatcher
from api.plugins.sentry.exceptions import (
    SentryAPIError,
    SentryAuthError,
    SentryNotFoundError,
    SentryRateLimitError,
)
from api.plugins.sentry.models import (
    SentryCodeMapping,
    SentryIssue,
    SentryOrganization,
    SentryProject,
    SentryRelease,
    SentryWebhookSubscription,
)
from api.plugins.sentry.watcher import SentryWatcher

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SentryPlugin(BaseHttpPlugin[SentryPluginConfig]):
    """Sentry API plugin.

    Provides typed API methods with Sentry-specific authentication,
    retry logic, rate-limit handling, and Link-header pagination.
    """

    plugin_name = "sentry"
    config_class = SentryPluginConfig
    priority = 30

    def __init__(self, plugin_config: SentryPluginConfig) -> None:
        super().__init__(plugin_config)
        self._rate_limit_until: dict[str, float] = {}
        self._dispatcher: SentryDispatcher | None = None
        self._watcher: SentryWatcher | None = None

    def _get_base_url(self, base_url: str | None = None) -> str:
        """Get the API base URL, optionally overriding the config value."""
        url = base_url or self.config.base_url
        return f"{url.rstrip('/')}/api/0"

    def _get_default_headers(self, auth_token: str | None = None) -> dict[str, str]:
        """Get default headers, optionally overriding the auth token."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = auth_token or self.config.auth_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def startup(self) -> None:
        """Start HTTP service, register container backend, and dispatch loop."""
        await super().startup()

        # Start auto-dispatch loop
        if self.config.dispatch.enabled:
            self._dispatcher = SentryDispatcher(self.config.dispatch)
            await self._dispatcher.start()

    async def watch(self) -> None:
        """Start the sentry container watcher.

        Called after all plugins have started, so FastStream and Container
        plugins are guaranteed ready.
        """
        if not self.config.watcher.enabled:
            return

        app = get_current_app()
        if not app.container or not app.faststream:
            logger.warning("Container or FastStream plugin not available, skipping watcher")
            return

        broker = app.faststream.get_broker()
        redis = app.faststream.get_redis()

        backend: ContainerBackend
        if app.container.config.backend == "kubernetes":
            k8s_config = app.container.config.kubernetes
            if not k8s_config:
                logger.warning("Kubernetes config not available, skipping watcher")
                return
            backend = KubernetesBackend("sentry", k8s_config, broker, redis)
        else:
            docker_config = app.container.config.docker
            if not docker_config:
                logger.warning("Docker config not available, skipping watcher")
                return
            backend = DockerBackend("sentry", docker_config, broker, redis)

        self._watcher = SentryWatcher(backend, self.config.watcher)
        await self._watcher.start()

    async def shutdown(self) -> None:
        """Stop watcher, dispatch loop, and HTTP service."""
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None
        if self._dispatcher:
            await self._dispatcher.stop()
            self._dispatcher = None
        await super().shutdown()

    async def health_check(self) -> dict[str, Any]:
        """Check Sentry plugin health including dispatch loop and watcher."""
        result: dict[str, Any] = {"healthy": True}
        if self._dispatcher:
            result["dispatch"] = self._dispatcher.health_check()
            if not result["dispatch"]["healthy"]:
                result["healthy"] = False
        if self._watcher:
            result["watcher"] = self._watcher.health_check()
            if not result["watcher"]["healthy"]:
                result["healthy"] = False
        return result

    # ── HTTP Transport ─────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        """Make an HTTP request with retry and rate-limit logic.

        When auth_token or base_url overrides are provided, builds a full URL
        and custom headers instead of relying on the httpx client defaults.
        """
        # When overrides are provided, build full URL and merge headers
        if auth_token or base_url:
            full_url = f"{self._get_base_url(base_url)}{path}"
            merged_headers = {**self._get_default_headers(auth_token), **(headers or {})}
        else:
            full_url = path
            merged_headers = headers or {}

        category = self._rate_limit_category(path)

        for attempt in range(self.config.max_retries + 1):
            await self._wait_for_rate_limit(category)

            try:
                response = await self.http.client.request(
                    method, full_url, params=params, json=json, headers=merged_headers
                )
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Sentry API transport error (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        self.config.max_retries + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                msg = f"Sentry API transport error after {self.config.max_retries + 1} attempts: {exc}"
                raise SentryAPIError(msg) from exc

            self._update_rate_limits(category, response)

            if response.status_code in (200, 201):
                return response.json()

            if response.status_code in (401, 403):
                msg = f"Sentry authentication error: {response.text}"
                raise SentryAuthError(msg, status_code=response.status_code)

            if response.status_code == 404:
                msg = f"Sentry resource not found: {path}"
                raise SentryNotFoundError(msg, status_code=404)

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.config.max_retries:
                delay = self._backoff_delay(attempt)
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                logger.warning(
                    "Sentry API %d (attempt %d/%d) for %s. Retrying in %.1fs",
                    response.status_code,
                    attempt + 1,
                    self.config.max_retries + 1,
                    path,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code == 429:
                retry_after = self._parse_retry_after(response)
                msg = f"Sentry rate limit exceeded for {path}"
                raise SentryRateLimitError(msg, retry_after=retry_after)

            msg = f"Sentry API error {response.status_code}: {response.text}"
            raise SentryAPIError(msg, status_code=response.status_code)

        msg = f"Sentry API request failed after {self.config.max_retries + 1} attempts"
        raise SentryAPIError(msg)

    async def _get_paginated(
        self,
        path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[Any]:
        """Fetch all pages of a paginated endpoint using Link headers."""
        all_results: list[Any] = []
        category = self._rate_limit_category(path)
        # httpx replaces (rather than merges into) a URL's existing query string
        # whenever `params` is passed explicitly, even as `{}` — so pagination
        # cursors embedded in Link-header URLs must never be paired with `params={}`.
        current_params = dict(params) if params else None

        # When overrides are provided, build full URL and merge headers
        if auth_token or base_url:
            url: str | None = f"{self._get_base_url(base_url)}{path}"
            merged_headers = {**self._get_default_headers(auth_token), **(headers or {})}
        else:
            url = path
            merged_headers = headers or {}

        while url:
            await self._wait_for_rate_limit(category)

            response = await self.http.client.request(
                "GET", url, params=current_params, headers=merged_headers
            )
            self._update_rate_limits(category, response)

            if response.status_code in (401, 403):
                msg = f"Sentry authentication error: {response.text}"
                raise SentryAuthError(msg, status_code=response.status_code)

            if response.status_code == 404:
                msg = f"Sentry resource not found: {path}"
                raise SentryNotFoundError(msg, status_code=404)

            if response.status_code != 200:
                msg = f"Sentry API error {response.status_code}: {response.text}"
                raise SentryAPIError(msg, status_code=response.status_code)

            data = response.json()
            if isinstance(data, list):
                all_results.extend(data)
            else:
                all_results.append(data)

            url = self._parse_next_link(response)
            current_params = None

        return all_results

    # ── Rate Limiting ──────────────────────────────────────────────────

    async def _wait_for_rate_limit(self, category: str) -> None:
        """Wait if we are rate-limited for the given category."""
        until = self._rate_limit_until.get(category, 0)
        now = time.monotonic()
        if until > now:
            delay = until - now
            logger.debug("Rate limited for category %r, waiting %.1fs", category, delay)
            await asyncio.sleep(delay)

    def _update_rate_limits(self, category: str, response: httpx.Response) -> None:
        """Parse X-Sentry-Rate-Limits header.

        Format: retry_after:categories:scope, retry_after:categories:scope, ...
        """
        header = response.headers.get("x-sentry-rate-limits")
        if not header:
            return

        now = time.monotonic()
        for limit in header.split(","):
            parts = limit.strip().split(":")
            if len(parts) < 1:
                continue
            try:
                retry_after = float(parts[0])
            except ValueError:
                continue
            self._rate_limit_until[category] = max(
                self._rate_limit_until.get(category, 0),
                now + retry_after,
            )

    # ── Pagination ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_next_link(response: httpx.Response) -> str | None:
        """Parse next page URL from Sentry's Link header."""
        link_header = response.headers.get("link")
        if not link_header:
            return None

        for part in link_header.split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                start = part.find("<")
                end = part.find(">")
                if start != -1 and end != -1:
                    return str(part[start + 1 : end])
        return None

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse Retry-After header value in seconds."""
        value = response.headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _rate_limit_category(path: str) -> str:
        """Derive a rate limit category from a request path."""
        parts = path.strip("/").split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else "default"

    def _backoff_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay for a retry attempt."""
        delay = self.config.backoff_base * (2**attempt)
        return float(min(delay, self.config.backoff_max))

    # ── Backend API Operations ─────────────────────────────────────────

    async def list_organizations(
        self,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[SentryOrganization]:
        """List organizations accessible to the auth token.

        Note: Does NOT work with internal integration tokens.
        Use get_organization() with an explicit slug instead.
        """
        data = await self._get_paginated(
            "/organizations/",
            params={"member": "true"},
            auth_token=auth_token,
            base_url=base_url,
        )
        return [SentryOrganization.model_validate(item) for item in data]

    async def get_organization(
        self,
        org_slug: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> SentryOrganization:
        """Get a specific organization by slug.

        Works with both user tokens and internal integration tokens.
        """
        data = await self._request(
            "GET",
            f"/organizations/{org_slug}/",
            auth_token=auth_token,
            base_url=base_url,
        )
        return SentryOrganization.model_validate(data)

    async def get_installation_uuid(
        self,
        org_slug: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> str | None:
        """Get the installation UUID for our integration on this org.

        Calls GET /api/0/organizations/{org_slug}/sentry-app-installations/
        and returns the UUID of the first installed integration.
        """
        data = await self._request(
            "GET",
            f"/organizations/{org_slug}/sentry-app-installations/",
            auth_token=auth_token,
            base_url=base_url,
        )
        if isinstance(data, list):
            for installation in data:
                if installation.get("status") == "installed":
                    return installation.get("uuid")
        return None

    async def list_projects(
        self,
        org_slug: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[SentryProject]:
        """List all projects in an organization."""
        data = await self._get_paginated(
            f"/organizations/{org_slug}/projects/",
            auth_token=auth_token,
            base_url=base_url,
        )
        return [SentryProject.model_validate(item) for item in data]

    async def list_code_mappings(
        self,
        org_slug: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[SentryCodeMapping]:
        """Get code mappings for an organization."""
        data = await self._get_paginated(
            f"/organizations/{org_slug}/code-mappings/",
            auth_token=auth_token,
            base_url=base_url,
        )
        return [SentryCodeMapping.model_validate(item) for item in data]

    async def list_issues(
        self,
        org_slug: str,
        project_id: str,
        query: str = "",
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[SentryIssue]:
        """List issues for a project, optionally filtered by query."""
        params: dict[str, str] = {"project": project_id}
        if query:
            params["query"] = query
        data = await self._get_paginated(
            f"/organizations/{org_slug}/issues/",
            params=params,
            auth_token=auth_token,
            base_url=base_url,
        )
        return [SentryIssue.model_validate(item) for item in data]

    async def get_issue(
        self,
        issue_id: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> SentryIssue:
        """Fetch issue details by ID."""
        data = await self._request(
            "GET", f"/issues/{issue_id}/", auth_token=auth_token, base_url=base_url
        )
        return SentryIssue.model_validate(data)

    async def register_webhook(
        self, org_slug: str, url: str, events: list[str]
    ) -> SentryWebhookSubscription:
        """Register a webhook subscription for an organization."""
        payload = {"url": url, "events": events}
        data = await self._request(
            "POST",
            f"/organizations/{org_slug}/integrations/webhook/",
            json=payload,
        )
        return SentryWebhookSubscription.model_validate(data)

    async def list_releases(
        self,
        org_slug: str,
        project_slug: str,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> list[SentryRelease]:
        """List releases for a project."""
        data = await self._get_paginated(
            f"/organizations/{org_slug}/releases/",
            params={"project": project_slug},
            auth_token=auth_token,
            base_url=base_url,
        )
        return [SentryRelease.model_validate(item) for item in data]
