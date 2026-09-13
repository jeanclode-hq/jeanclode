"""Shared utilities for container backends."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from api.context import get_current_app

if TYPE_CHECKING:
    from uuid import UUID

    from api.models.issues import Issue


class ThirdPartyPluginsResolved(BaseModel):
    """Resolved third-party plugin specs split for the launcher.

    ``env`` goes into the agent's public env (the CLI reads it to know
    which plugins to clone). ``extra_hosts`` are the git hosts those
    plugins live on — the launcher adds them to the proxy allowlist.
    ``git_urls`` are the repos themselves, which the launcher hands to
    ``add_plugin_marketplace_credentials`` so an internal or private plugin
    repo gets the org's token injected on its own path (allowlisting the
    host only lets the request through unauthenticated).
    """

    env: dict[str, str] = Field(default_factory=dict)
    extra_hosts: list[str] = Field(default_factory=list)
    git_urls: list[str] = Field(default_factory=list)


logger = logging.getLogger(__name__)

# Regex patterns for parsing structured logs from CLI containers
RESULT_PATTERN = re.compile(r"\[JEANCLODE:RESULT\]\s*(.+)")
ERROR_PATTERN = re.compile(r"\[JEANCLODE:ERROR\]\s*(.+)")

# Label keys used to identify jeanclode containers
LABEL_EXECUTION_ID = "jeanclode.execution_id"
LABEL_PLUGIN = "jeanclode.plugin"
LABEL_ROLE = "jeanclode.role"
# On a sandboxed agent container (Docker backend): the name of its
# security-proxy sidecar, so exit/cleanup can tear the pair down together.
LABEL_PROXY_CONTAINER = "jeanclode.proxy_container"


def build_container_labels(
    execution_id: str,
    plugin_name: str,
    role: str = "cli",
) -> dict[str, str]:
    """Build standard container labels for tracking and filtering.

    Args:
        execution_id: Unique execution identifier.
        plugin_name: Plugin that owns this container (e.g., "sentry").
        role: What kind of container (default "cli").

    Returns:
        Dictionary of labels to apply to the container.
    """
    return {
        LABEL_EXECUTION_ID: execution_id,
        LABEL_PLUGIN: plugin_name,
        LABEL_ROLE: role,
    }


def parse_structured_logs(
    log_text: str,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None, list[dict[str, Any]]]:
    """Parse structured output from container logs.

    Looks for special markers in the logs:
    - [JEANCLODE:RESULT] {...} — JSON result data
    - [JEANCLODE:ERROR] {...} — JSON or plain text error

    Args:
        log_text: Full log output from container.

    Returns:
        Tuple of (result dict or None, error info tuple (error_type,
        error_message) or None, rate_limit_events list). The third element
        collects every ``[JEANCLODE:ERROR]`` payload whose ``type`` is
        ``rate_limit_error`` — a single container can run multiple groups
        concurrently (e.g. ``sentry_fix``'s parallel synthesis groups), each
        independently hitting the shared credential's rate limit and
        emitting its own event with fields (``credential_id``,
        ``retry_after``, ``branch``, ``pr_url``). Keeping all of them (not
        just the last) lets callers clean up every affected group, not only
        one. ``error_info`` deliberately doesn't carry these fields, since
        every other caller only ever needs the generic (type, message) pair
        (ADR-010).
    """
    result: dict[str, Any] | None = None
    error_info: tuple[str, str] | None = None
    rate_limit_events: list[dict[str, Any]] = []

    for line in log_text.split("\n"):
        result_match = RESULT_PATTERN.search(line)
        if result_match:
            try:
                result = json.loads(result_match.group(1))
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse result JSON: {result_match.group(1)}")

        error_match = ERROR_PATTERN.search(line)
        if error_match:
            try:
                error_data = json.loads(error_match.group(1))
                error_type = error_data.get("type", "internal_error")
                error_message = error_data.get("message", str(error_data))
                error_info = (error_type, error_message)
                if error_type == "rate_limit_error":
                    rate_limit_events.append(error_data)
            except json.JSONDecodeError:
                error_info = ("internal_error", error_match.group(1))

    return result, error_info, rate_limit_events


def determine_execution_status(
    exit_code: int,
    error_info: tuple[str, str] | None,
) -> tuple[str, tuple[str, str] | None]:
    """Determine execution status from container exit code.

    Exit code is the source of truth — a successful run that we couldn't
    parse (pod GC'd before reconcile, log marker malformed, fetch glitch)
    is still successful.

    Args:
        exit_code: Container exit code (0 = success).
        error_info: Parsed error info tuple (error_type, error_message) or None.

    Returns:
        Tuple of (status string, error info tuple or None).
    """
    if exit_code == 0:
        return "completed", None

    if error_info:
        return "failed", error_info

    return "failed", ("container_error", f"Container exited with code {exit_code}")


def resolve_git_org_id(issues: list[Issue]) -> UUID | None:
    """Resolve the git org ID from an issue's mapped repository.

    Follows Issue → Repository → mapped_repo → Organization. This is the
    shared pattern every dispatch path uses to find the git org associated
    with an issue (Sentry, Linear, git issues, etc.).
    """
    from api.database.organization import db_get_org_by_id

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return None

    with db_plugin.session() as db:
        issue = issues[0]
        repo = issue.repository
        if not repo or not repo.mapped_repo_id:
            return None
        mapped_repo = repo.mapped_repo
        if not mapped_repo:
            return None
        git_org = db_get_org_by_id(db, mapped_repo.org_id)
        return git_org.id if git_org else None


async def _get_dispatch_git_token(org_id: UUID) -> str | None:
    """Get the git org's auth token for marketplace manifest fetches at dispatch."""
    from api.database.organization import db_get_org_by_id

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return None

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        if not org:
            return None
        provider = org.provider
        installation_id = org.installation_id
        encrypted_token = org.auth_token_encrypted

    if provider == "github" and installation_id and app.github:
        try:
            return await app.github.get_installation_access_token(installation_id)
        except Exception:
            logger.warning("Failed to get GitHub token for dispatch plugin resolve")
            return None

    if provider == "gitlab" and encrypted_token and db_plugin:
        try:
            return db_plugin.decrypt(encrypted_token)
        except Exception:
            logger.warning("Failed to decrypt GitLab token for dispatch plugin resolve")
            return None

    return None


async def resolve_third_party_plugins_env(issues: list[Issue]) -> ThirdPartyPluginsResolved:
    """Resolve third-party plugins for an issue-based dispatch.

    Plugins are resolved from the git org (not the source org). The CLI in
    the agent container reads ``JEANCLODE_THIRDPARTY_PLUGINS`` and clones
    each spec at runtime; the proxy denies any host that isn't allowlisted,
    so we extract every plugin's git host here and hand them to the launcher
    as ``extra_hosts``.
    """
    git_org_id = resolve_git_org_id(issues)
    if not git_org_id:
        return ThirdPartyPluginsResolved()
    return await resolve_third_party_plugins_env_for_org(git_org_id, workflow="fix")


async def resolve_third_party_plugins_env_for_org(
    git_org_id: UUID,
    *,
    workflow: str,
) -> ThirdPartyPluginsResolved:
    """Resolve third-party plugins for a dispatch keyed by git org id.

    Same shape as :func:`resolve_third_party_plugins_env` but takes the
    git org id directly so PR/MR dispatch (which already knows the git
    org from ``pr.repository.org_id``) can avoid the issue → mapped_repo
    walk.
    """
    from api.routers.marketplaces.utils import read_plugin_install_rows, resolve_plugin_specs

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return ThirdPartyPluginsResolved()

    auth_token = await _get_dispatch_git_token(git_org_id)

    try:
        rows = await db_plugin.run_in_session(
            lambda db: read_plugin_install_rows(db, git_org_id, workflow=workflow)
        )
        specs = await resolve_plugin_specs(rows, auth_token=auth_token)
    except Exception:
        logger.exception("Failed to resolve third-party plugins for org %s", git_org_id)
        return ThirdPartyPluginsResolved()

    if not specs:
        return ThirdPartyPluginsResolved()

    payload = json.dumps([s.model_dump(exclude_none=True) for s in specs])
    hosts: set[str] = set()
    for spec in specs:
        parsed = urlparse(spec.git_url)
        if parsed.hostname:
            hosts.add(parsed.hostname.lower())

    return ThirdPartyPluginsResolved(
        env={
            "JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1",
            "JEANCLODE_THIRDPARTY_PLUGINS": payload,
        },
        extra_hosts=sorted(hosts),
        git_urls=sorted({s.git_url for s in specs if s.git_url}),
    )


def parse_memory_limit(memory_str: str) -> int:
    """Parse memory limit string to bytes.

    Supports common suffixes: k/K (kilobytes), m/M (megabytes), g/G (gigabytes).

    Args:
        memory_str: Memory string like '2g', '512m', '1024k', or raw bytes.

    Returns:
        Memory in bytes.
    """
    memory_str = memory_str.strip().lower()
    units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    if memory_str[-1] in units:
        return int(float(memory_str[:-1]) * units[memory_str[-1]])
    return int(memory_str)
