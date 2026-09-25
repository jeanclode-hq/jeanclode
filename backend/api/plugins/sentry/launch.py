"""Reusable container launch logic for Sentry-driven CLI dispatch.

Shared by both auto-dispatch (SentryDispatcher) and manual fix trigger.

Sandboxing is unconditional. The dispatcher resolves three credential
sources from authoritative configuration (per-org for Sentry, admin LLM
config for the model, per-mapped-org for git platform tokens) and hands
the K8s backend explicit ``UpstreamCredential`` entries — the security
proxy then knows exactly which hosts to allow and how to inject auth.

Cross-plugin credential resolvers (LLM, git platform, agent tooling
hosts) live in :mod:`api.plugins.container.dispatch_inputs`. This module
keeps only Sentry-specific resolution: the per-org auth token, the
SaaS-region host allowlist, and the CLI command shape.
"""

import logging
from uuid import UUID

from api.context import get_current_app
from api.database.execution import (
    db_update_execution_container,
    db_update_execution_status,
)
from api.database.llm_credentials import LLMCredentialAvailability
from api.database.repository import db_get_related_repos
from api.models.executions import ExecutionStatus
from api.models.issues import Issue
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.plugins.container.backend import ContainerRequest
from api.plugins.container.dispatch_inputs import (
    DispatchInputs,
    LLMSelectionResult,
    add_agent_tooling_hosts,
    add_connectors_to_inputs,
    add_git_platform_to_inputs,
    add_gitlab_workspace_credentials,
    add_llm_to_inputs,
    add_memory_to_inputs,
    add_plugin_marketplace_credentials,
    host_or,
)
from api.plugins.container.security_proxy import CredentialKey, UpstreamCredential
from api.plugins.container.sizing import resolve_tmp_size_limit
from api.plugins.container.utils import build_container_labels, resolve_third_party_plugins_env

logger = logging.getLogger(__name__)


# Sentry SaaS routes orgs to one of these regional API hosts; the CLI
# discovers the right one at runtime from `links.regionUrl`. We allowlist
# all of them up front when the org is on SaaS so the agent doesn't trip
# the proxy mid-flight.
_SENTRY_SAAS_HOSTS = ("sentry.io", "us.sentry.io", "de.sentry.io", "eu.sentry.io")


def build_sentry_command(
    issues: list[Issue],
    org: Organization,
    related_repos: list[Repository] | None = None,
) -> list[str]:
    """Build the agent CLI command — Sentry URLs interleaved with --repo overrides.

    The CLI's resolver would otherwise hit Sentry's code-mappings, which can
    point at a different repo than the user's manual mapping in our DB.
    Passing --repo per issue forces the CLI to use the mapped repo's web_url.

    ``related_repos`` (repos grouped with the mapped repo via
    ``RepositoryMapping``) get one ``--related-repo`` each, right after the
    ``--repo`` they apply to — the CLI parser binds trailing flags to the
    URL(s) that precede them, so these must stay adjacent to this issue's
    URL rather than trailing at the end of the whole command.

    Each issue is addressed by the permalink Sentry itself gave us at
    ingestion (``Issue.issue_url``), because that URL is not ours to
    invent: on self-hosted the real one is org-scoped
    (``/organizations/<org>/issues/<id>/``) and the flat
    ``<base>/issues/<id>/`` we used to synthesize ends up quoted in every
    PR/MR body and Sentry comment. The synthesized form stays as a
    fallback for rows whose permalink the webhook didn't carry.
    """
    sentry_base = org.base_url.rstrip("/") if org.base_url else "https://sentry.io"
    slug = org.external_org_id
    url_prefix = f"https://{slug}.sentry.io" if sentry_base == "https://sentry.io" else sentry_base

    cmd: list[str] = []
    for issue in issues:
        cmd.append(issue.issue_url or f"{url_prefix}/issues/{issue.external_id}/")
        repo = issue.repository
        mapped = repo.mapped_repo if repo else None
        if mapped and mapped.web_url:
            cmd.extend(["--repo", mapped.web_url])
            for related in related_repos or []:
                if related.web_url:
                    cmd.extend(["--related-repo", related.web_url])
    return cmd


def _resolve_mapped_repo(issues: list[Issue]) -> Repository | None:
    """The first issue's mapped repo, if any.

    Mirrors ``build_dispatch_inputs``'s single-issue assumption — a batch
    is always issues for the same Sentry project, so they share one mapped
    repo and one repo group.
    """
    issue = issues[0]
    repository = issue.repository
    return repository.mapped_repo if repository else None


def _resolve_mapped_repos(issues: list[Issue]) -> list[Repository]:
    """Every distinct mapped repo in the batch, in issue order.

    ``build_sentry_command`` emits a ``--repo`` per issue, so the CLI clones
    each one — sizing has to price the same set. ``_resolve_mapped_repo``'s
    same-project assumption holds for the repo *group*, not for the disk a
    batch spanning several mapped repos actually writes.
    """
    seen: dict[UUID, Repository] = {}
    for issue in issues:
        repository = issue.repository
        mapped = repository.mapped_repo if repository else None
        if mapped:
            seen.setdefault(mapped.id, mapped)
    return list(seen.values())


def _resolve_related_repos(issues: list[Issue]) -> list[Repository]:
    """Repos grouped with the first issue's mapped repo, if any."""
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return []

    mapped_repo = _resolve_mapped_repo(issues)
    if not mapped_repo:
        return []

    with db_plugin.session() as db:
        return db_get_related_repos(db, mapped_repo.id)


async def build_dispatch_inputs(
    issues: list[Issue],
    org: Organization,
    related_repos: list[Repository] | None = None,
    *,
    workspace_id: UUID | None = None,
    execution_id: UUID | None = None,
) -> tuple[DispatchInputs, LLMSelectionResult]:
    """Resolve everything the agent needs from authoritative configuration.

    Returns the public env (URLs, model names, third-party plugin env),
    the secret env (credential values, sidecar-only), and the proxy
    upstream rules (one per allowed host with its injection metadata),
    alongside the LLM credential selection outcome — the caller decides
    what to do when it isn't ``AVAILABLE`` (ADR-010).

    ``workspace_id`` non-``None`` is itself the gate for wiring the memory credential.
    """
    inputs = DispatchInputs()
    _add_sentry(inputs, org)
    llm_selection = add_llm_to_inputs(inputs, fixer_options=True)

    issue = issues[0]
    repository = issue.repository
    mapped_repo = repository.mapped_repo if repository else None
    if mapped_repo:
        # A related repo can live in a different GitLab group than the
        # primary — GitLab group tokens are scoped to one group each (no
        # GitHub-App-style umbrella credential), so the single-org token
        # add_git_platform_to_inputs injects wouldn't authorize a clone
        # there. Only pay for the workspace-wide, path-prefix-scoped
        # lookup when a related repo actually crosses that boundary.
        spans_other_org = mapped_repo.provider == "gitlab" and any(
            r.org_id != mapped_repo.org_id for r in (related_repos or [])
        )
        if spans_other_org:
            await add_gitlab_workspace_credentials(
                inputs, git_org_id=mapped_repo.org_id, execution_id=execution_id
            )
        else:
            await add_git_platform_to_inputs(
                inputs,
                git_org_id=mapped_repo.org_id,
                repo_token_override_encrypted=mapped_repo.auth_token_encrypted,
            )
        await add_connectors_to_inputs(inputs, git_org_id=mapped_repo.org_id)
    else:
        logger.info(
            "git platform creds skipped: issue %s has no mapped repository",
            issue.id,
        )

    add_agent_tooling_hosts(inputs)

    plugins = await resolve_third_party_plugins_env(issues)
    inputs.public_env.update(plugins.env)
    inputs.extra_hosts.extend(plugins.extra_hosts)
    if mapped_repo:
        await add_plugin_marketplace_credentials(
            inputs, git_org_id=mapped_repo.org_id, git_urls=plugins.git_urls
        )

    if workspace_id is not None and execution_id is not None:
        await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

    return inputs, llm_selection


def _add_sentry(inputs: DispatchInputs, org: Organization) -> None:
    """Sentry credential lives on the org row; URL too (self-hosted support).

    Falls back to plugin-level config for installations that pre-date the
    per-org credential model.
    """
    app = get_current_app()
    db_plugin = app.database

    token: str | None = None
    base_url: str | None = None

    if org.auth_token_encrypted and db_plugin:
        token = db_plugin.decrypt(org.auth_token_encrypted)
        base_url = org.base_url or "https://sentry.io"
    elif app.sentry and app.sentry.config.auth_token:
        token = app.sentry.config.auth_token
        base_url = app.sentry.config.base_url

    if not token:
        return

    inputs.secrets[CredentialKey.SENTRY_AUTH_TOKEN] = token
    if base_url:
        inputs.public_env["SENTRY_API_URL"] = base_url

    resolved_host = host_or("sentry.io", base_url)
    # Sentry SaaS auto-routes to a regional API host (de/us/eu) per org.
    # The CLI hits sentry.io first, learns the region, then re-targets —
    # so we must allowlist every region up front. Self-hosted installs
    # have a single fixed host and skip this branch.
    hosts = _SENTRY_SAAS_HOSTS if resolved_host == "sentry.io" else (resolved_host,)
    for host in hosts:
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=CredentialKey.SENTRY_AUTH_TOKEN,
                host=host,
                header="Authorization",
                bearer=True,
            )
        )


async def launch_container(
    issues: list[Issue],
    org: Organization,
    execution_id: UUID,
    *,
    workspace_id: UUID | None = None,
) -> str | None:
    """Launch a sandboxed CLI container and update the execution record.

    One container per execution, regardless of how many issues are batched.
    The K8s backend always sandboxes — credentials never reach the agent
    container.

    ``workspace_id``, when set, wires the memory credential (caller confirms opt-in first).
    """
    app = get_current_app()
    db_plugin = app.database
    sentry_plugin = app.sentry

    if not sentry_plugin or not sentry_plugin._watcher:
        logger.error("Sentry watcher not available, cannot dispatch")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="no_watcher",
                    error_detail="Sentry watcher not available",
                )
        return None

    backend = sentry_plugin._watcher.backend
    related_repos = _resolve_related_repos(issues)
    inputs, llm_selection = await build_dispatch_inputs(
        issues,
        org,
        related_repos=related_repos,
        workspace_id=workspace_id,
        execution_id=execution_id,
    )

    # Misconfiguration (an empty pool) is not exhaustion — fail fast, same
    # shape as the "no_watcher" case above. Real, temporary exhaustion
    # never reaches here: SentryDispatcher's tick already skips creating an
    # execution at all when every credential is stale (ADR-010).
    if llm_selection.availability == LLMCredentialAvailability.NONE_CONFIGURED:
        logger.error("No LLM credentials configured, cannot dispatch")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="no_llm_credentials_configured",
                    error_detail="No LLM credentials are configured",
                )
        return None

    container_plugin = app.container
    image = "jeanclode/cli:latest"
    timeout = 1800
    if container_plugin:
        if container_plugin.config.backend == "kubernetes" and container_plugin.config.kubernetes:
            image = container_plugin.config.kubernetes.image
            timeout = container_plugin.config.kubernetes.timeout
        elif container_plugin.config.docker:
            image = container_plugin.config.docker.image
            timeout = container_plugin.config.docker.timeout

    cli_command = build_sentry_command(issues, org, related_repos=related_repos)
    size_repos = [*_resolve_mapped_repos(issues), *related_repos]
    tmp_size_limit = await resolve_tmp_size_limit(
        size_repos[0] if size_repos else None, size_repos[1:]
    )

    request = ContainerRequest(
        image=image,
        command=cli_command,
        env=inputs.public_env,
        secrets=inputs.secrets,
        upstreams=inputs.upstreams,
        oauth_upstreams=inputs.oauth_upstreams,
        extra_hosts=inputs.extra_hosts,
        timeout_seconds=timeout,
        tmp_size_limit=tmp_size_limit,
        labels=build_container_labels(
            execution_id=str(execution_id),
            plugin_name="sentry",
        ),
    )

    try:
        container_id = await backend.start_container(
            execution_id=str(execution_id),
            request=request,
        )
        logger.info(
            "Dispatched container %s for execution %s (issues=%d, upstreams=%d)",
            container_id[:12],
            str(execution_id)[:8],
            len(issues),
            len(inputs.upstreams),
        )
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_container(db, execution_id, container_id)
        return container_id
    except Exception:
        logger.exception("Failed to dispatch container, marking execution as failed")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="container_start_failed",
                    error_detail="Failed to start container",
                )
        return None
