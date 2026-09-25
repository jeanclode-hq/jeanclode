"""Reusable container launch logic for GitLab-driven CLI dispatch.

Used by both the manual review/summary trigger and the webhook-driven
auto-trigger (MR open / push / labeled). Mirrors
:mod:`api.plugins.github.launch` exactly — only the watcher attribute and
the plugin label differ.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from api.context import get_current_app
from api.database.execution import (
    db_mark_execution_scheduled,
    db_update_execution_container,
    db_update_execution_status,
)
from api.database.llm_credentials import LLMCredentialAvailability
from api.models.executions import ExecutionStatus, ExecutionWorkflow
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.plugins.container.backend import ContainerRequest
from api.plugins.container.dispatch_inputs import (
    DispatchInputs,
    LLMSelectionResult,
    add_agent_tooling_hosts,
    add_connectors_to_inputs,
    add_gitlab_workspace_credentials,
    add_llm_to_inputs,
    add_memory_to_inputs,
    add_notify_to_inputs,
    add_plugin_marketplace_credentials,
)
from api.plugins.container.related import resolve_related_repos
from api.plugins.container.sizing import resolve_tmp_size_limit
from api.plugins.container.utils import (
    build_container_labels,
    resolve_third_party_plugins_env_for_org,
)
from api.plugins.database.plugin import DatabasePlugin

logger = logging.getLogger(__name__)


def _handle_llm_unavailable(
    db_plugin: DatabasePlugin | None,
    execution_id: UUID,
    llm_selection: LLMSelectionResult,
) -> bool:
    """Admit an execution as FAILED or SCHEDULED per the LLM selection outcome.

    Returns True if the caller should stop (credential unavailable in some
    way); False if ``llm_selection`` was actually AVAILABLE and dispatch
    should proceed as normal.
    """
    if llm_selection.available:
        return False

    if not db_plugin:
        return True

    with db_plugin.session() as db:
        if llm_selection.availability == LLMCredentialAvailability.NONE_CONFIGURED:
            logger.error("No LLM credentials configured, cannot dispatch")
            db_update_execution_status(
                db,
                execution_id,
                ExecutionStatus.FAILED.value,
                error_type="no_llm_credentials_configured",
                error_detail="No LLM credentials are configured",
            )
        else:
            retry_at = llm_selection.retry_at or datetime.now(UTC)
            logger.info(
                "Every LLM credential is stale, scheduling execution %s for retry at %s",
                execution_id,
                retry_at,
            )
            db_mark_execution_scheduled(db, execution_id, retry_at=retry_at)
    return True


# Workflows that can post the "ready for you" ping. Both are ends of the
# autonomous review loop: REVIEW when it converges on LGTM, RESPOND when a
# resolve-only turn clears the last thread without a push.
_NOTIFY_WORKFLOWS = (ExecutionWorkflow.REVIEW, ExecutionWorkflow.RESPOND)


def build_command(workflow: ExecutionWorkflow, pr: PullRequest) -> list[str]:
    """Build the agent CLI command for an MR-driven workflow."""
    if workflow == ExecutionWorkflow.SUMMARY:
        return ["summary", pr.pr_url]
    if workflow == ExecutionWorkflow.RESPOND:
        return ["respond", pr.pr_url]
    return [pr.pr_url]


def build_respond_command(target_url: str, related_repo_urls: list[str] | None = None) -> list[str]:
    """Build the CLI command for a mention-driven respond run.

    ``related_repo_urls`` are cloned read-only alongside the mention's own
    repo so the planner can answer questions that reach into a grouped
    repo (e.g. a monorepo the mention's repo is only a QA harness for) —
    the prompt guardrail (not this command) is what keeps ``handle`` from
    writing to anything but the mention's own repo. A change that spans
    repos still has to go through ``route`` -> ``jeanclode:resolve``.
    """
    cmd = ["respond", target_url]
    for url in related_repo_urls or []:
        cmd.extend(["--related-repo", url])
    return cmd


def build_issue_resolve_command(
    issue_url: str, related_repo_urls: list[str] | None = None
) -> list[str]:
    """Build the CLI command for an issue-resolve run.

    ``related_repo_urls`` (repos grouped with the issue's repo via
    ``RepositoryMapping``) each get a ``--related-repo`` flag so the CLI
    clones them alongside the issue's own repo.
    """
    cmd = ["issue-resolve", issue_url]
    for url in related_repo_urls or []:
        cmd.extend(["--related-repo", url])
    return cmd


def _resolve_related_repos(repo: Repository | None) -> list[Repository]:
    """Repos cloned alongside ``repo`` — groups, always-include, subgroup pack."""
    if repo is None:
        return []
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return []
    with db_plugin.session() as db:
        return resolve_related_repos(db, repo)


async def build_dispatch_inputs(
    workflow: ExecutionWorkflow,
    pr: PullRequest,
    *,
    workspace_id: UUID | None = None,
    execution_id: UUID | None = None,
) -> tuple[DispatchInputs, LLMSelectionResult]:
    """Resolve LLM + git platform credentials + agent tooling hosts.

    Third-party skills are filtered by ``workflow`` so opt-in plugins
    only join the run for the workflow they support.

    ``workspace_id`` non-``None`` is itself the gate for wiring the memory credential.
    Returns the LLM selection outcome alongside the inputs — see
    :func:`_handle_llm_unavailable`.
    """
    inputs = DispatchInputs()
    llm_selection = add_llm_to_inputs(inputs)

    repository = pr.repository
    if repository:
        await add_gitlab_workspace_credentials(
            inputs, git_org_id=repository.org_id, execution_id=execution_id
        )
    else:
        logger.warning("git platform creds skipped: MR %s has no repository", pr.id)

    add_agent_tooling_hosts(inputs)

    if repository:
        plugins = await resolve_third_party_plugins_env_for_org(
            repository.org_id, workflow=workflow.value
        )
        inputs.public_env.update(plugins.env)
        inputs.extra_hosts.extend(plugins.extra_hosts)
        await add_plugin_marketplace_credentials(
            inputs, git_org_id=repository.org_id, git_urls=plugins.git_urls
        )
        await add_connectors_to_inputs(inputs, git_org_id=repository.org_id)
        if workflow in _NOTIFY_WORKFLOWS:
            add_notify_to_inputs(inputs, git_org_id=repository.org_id)

    if workspace_id is not None and execution_id is not None:
        await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

    return inputs, llm_selection


async def launch_container(
    workflow: ExecutionWorkflow,
    pr: PullRequest,
    execution_id: UUID,
    *,
    workspace_id: UUID | None = None,
) -> str | None:
    """Launch a sandboxed CLI container for an MR workflow.

    ``workspace_id``, when set, wires the memory credential (caller confirms opt-in first).
    """
    app = get_current_app()
    db_plugin = app.database
    gitlab_plugin = app.gitlab

    if not gitlab_plugin or not gitlab_plugin._watcher:
        logger.error("GitLab watcher not available, cannot dispatch")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="no_watcher",
                    error_detail="GitLab watcher not available",
                )
        return None

    backend = gitlab_plugin._watcher.backend
    inputs, llm_selection = await build_dispatch_inputs(
        workflow, pr, workspace_id=workspace_id, execution_id=execution_id
    )
    if _handle_llm_unavailable(db_plugin, execution_id, llm_selection):
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

    cli_command = build_command(workflow, pr)
    tmp_size_limit = await resolve_tmp_size_limit(pr.repository)

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
            plugin_name="gitlab",
        ),
    )

    try:
        container_id = await backend.start_container(
            execution_id=str(execution_id),
            request=request,
        )
        logger.info(
            "Dispatched gitlab container %s for execution %s (workflow=%s, mr=%s, upstreams=%d)",
            container_id[:12],
            str(execution_id)[:8],
            workflow.value,
            pr.pr_url,
            len(inputs.upstreams),
        )
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_container(db, execution_id, container_id)
        return container_id
    except Exception:
        logger.exception("Failed to dispatch gitlab container, marking execution as failed")
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


async def launch_issue_resolve_container(
    *,
    issue_url: str,
    org_id: UUID,
    repo: Repository | None = None,
    execution_id: UUID,
    workspace_id: UUID | None = None,
) -> str | None:
    """Launch a sandboxed CLI container for an issue-resolve run on GitLab.

    Mirror of the GitHub launcher: the container's only argument is the
    issue URL; the CLI fetches body/comments from the GitLab API itself.
    ``repo`` (the issue's own repo, if known) is used to resolve any
    grouped related repos to pass along as ``--related-repo`` flags.

    ``workspace_id``, when set, wires the memory credential (caller confirms opt-in first).
    """
    app = get_current_app()
    db_plugin = app.database
    gitlab_plugin = app.gitlab

    if not gitlab_plugin or not gitlab_plugin._watcher:
        logger.error("GitLab watcher not available, cannot dispatch issue-resolve")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="no_watcher",
                    error_detail="GitLab watcher not available",
                )
        return None

    backend = gitlab_plugin._watcher.backend

    inputs = DispatchInputs()
    llm_selection = add_llm_to_inputs(inputs, fixer_options=True)
    if _handle_llm_unavailable(db_plugin, execution_id, llm_selection):
        return None
    await add_gitlab_workspace_credentials(inputs, git_org_id=org_id, execution_id=execution_id)
    add_agent_tooling_hosts(inputs)

    plugins = await resolve_third_party_plugins_env_for_org(
        org_id, workflow=ExecutionWorkflow.ISSUE_RESOLVE.value
    )
    inputs.public_env.update(plugins.env)
    inputs.extra_hosts.extend(plugins.extra_hosts)
    await add_plugin_marketplace_credentials(inputs, git_org_id=org_id, git_urls=plugins.git_urls)
    await add_connectors_to_inputs(inputs, git_org_id=org_id)

    if workspace_id is not None:
        await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

    related_repos = _resolve_related_repos(repo)
    related_repo_urls = [r.web_url for r in related_repos if r.web_url]

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

    tmp_size_limit = await resolve_tmp_size_limit(repo, related_repos)

    request = ContainerRequest(
        image=image,
        command=build_issue_resolve_command(issue_url, related_repo_urls),
        env=inputs.public_env,
        secrets=inputs.secrets,
        upstreams=inputs.upstreams,
        oauth_upstreams=inputs.oauth_upstreams,
        extra_hosts=inputs.extra_hosts,
        timeout_seconds=timeout,
        tmp_size_limit=tmp_size_limit,
        labels=build_container_labels(
            execution_id=str(execution_id),
            plugin_name="gitlab",
        ),
    )

    try:
        container_id = await backend.start_container(
            execution_id=str(execution_id),
            request=request,
        )
        logger.info(
            "Dispatched gitlab issue-resolve container %s for execution %s (issue=%s)",
            container_id[:12],
            str(execution_id)[:8],
            issue_url,
        )
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_container(db, execution_id, container_id)
        return container_id
    except Exception:
        logger.exception("Failed to dispatch gitlab issue-resolve container")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="container_start_failed",
                    error_detail="Failed to start issue-resolve container",
                )
        return None


async def launch_respond_container(
    *,
    target_url: str,
    org_id: UUID,
    repo: Repository | None = None,
    execution_id: UUID,
    workspace_id: UUID | None = None,
) -> str | None:
    """Launch a sandboxed CLI container for an @jeanclode mention on GitLab.

    Mirror of the GitHub launcher: the container's only argument is the
    note URL; the CLI fetches body/surface metadata from the GitLab API.
    ``repo`` (the mention's own repo, if known) also resolves related
    repos to clone alongside it, same as issue-resolve. A mention that
    needs a cross-repo *change* still has to route to ``jeanclode:resolve``.

    ``workspace_id``, when set, wires the memory credential (caller confirms opt-in first).
    """
    app = get_current_app()
    db_plugin = app.database
    gitlab_plugin = app.gitlab

    if not gitlab_plugin or not gitlab_plugin._watcher:
        logger.error("GitLab watcher not available, cannot dispatch respond")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="no_watcher",
                    error_detail="GitLab watcher not available",
                )
        return None

    backend = gitlab_plugin._watcher.backend

    inputs = DispatchInputs()
    llm_selection = add_llm_to_inputs(inputs)
    if _handle_llm_unavailable(db_plugin, execution_id, llm_selection):
        return None
    await add_gitlab_workspace_credentials(inputs, git_org_id=org_id, execution_id=execution_id)
    add_agent_tooling_hosts(inputs)

    add_notify_to_inputs(inputs, git_org_id=org_id)

    plugins = await resolve_third_party_plugins_env_for_org(
        org_id, workflow=ExecutionWorkflow.RESPOND.value
    )
    inputs.public_env.update(plugins.env)
    inputs.extra_hosts.extend(plugins.extra_hosts)
    await add_plugin_marketplace_credentials(inputs, git_org_id=org_id, git_urls=plugins.git_urls)
    await add_connectors_to_inputs(inputs, git_org_id=org_id)

    if workspace_id is not None:
        await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

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

    related_repos = _resolve_related_repos(repo)
    related_repo_urls = [r.web_url for r in related_repos if r.web_url]
    tmp_size_limit = await resolve_tmp_size_limit(repo, related_repos)

    request = ContainerRequest(
        image=image,
        command=build_respond_command(target_url, related_repo_urls),
        env=inputs.public_env,
        secrets=inputs.secrets,
        upstreams=inputs.upstreams,
        oauth_upstreams=inputs.oauth_upstreams,
        extra_hosts=inputs.extra_hosts,
        timeout_seconds=timeout,
        tmp_size_limit=tmp_size_limit,
        labels=build_container_labels(
            execution_id=str(execution_id),
            plugin_name="gitlab",
        ),
    )

    try:
        container_id = await backend.start_container(
            execution_id=str(execution_id),
            request=request,
        )
        logger.info(
            "Dispatched gitlab respond container %s for execution %s (target=%s)",
            container_id[:12],
            str(execution_id)[:8],
            target_url,
        )
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_container(db, execution_id, container_id)
        return container_id
    except Exception:
        logger.exception("Failed to dispatch gitlab respond container")
        if db_plugin:
            with db_plugin.session() as db:
                db_update_execution_status(
                    db,
                    execution_id,
                    ExecutionStatus.FAILED.value,
                    error_type="container_start_failed",
                    error_detail="Failed to start respond container",
                )
        return None
