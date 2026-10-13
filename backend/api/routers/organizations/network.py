"""Every host a run of an org can reach, for the "Network access" section.

Mirrors what the launchers put on the security proxy's allowlist, read from
the same rows and constants, so a tenant can see in one place what an agent
can reach and why.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from api.context import get_current_app
from api.database.connectors import db_get_credentials_by_org, db_get_mcp_servers_by_org
from api.database.organization import (
    db_get_connection_org_id,
    db_get_orgs_by_workspace,
    db_resolve_org_settings,
)
from api.database.plugins import db_get_installations_by_org, db_get_marketplace_by_id
from api.models.connectors import AuthType, SubjectType
from api.models.executions import ExecutionWorkflow
from api.models.organizations import Organization
from api.models.settings import GitOrgSettings
from api.plugins.container.dispatch_inputs import (
    AGENT_TOOLING_HOSTS,
    BROWSER_TOOLING_HOSTS,
    DEMO_TOOLING_HOSTS,
    SENTRY_SAAS_HOSTS,
    default_llm_host,
    host_or,
)

from .schemas import NetworkHostResponse, NetworkHostSource

_BROWSER_WORKFLOWS = [ExecutionWorkflow.ISSUE_RESOLVE.value, ExecutionWorkflow.RESPOND.value]

_SOURCE_ORDER = list(NetworkHostSource)


def _git_hosts(org: Organization) -> list[str]:
    if org.provider == "github":
        base = host_or("github.com", org.base_url)
        return ["api.github.com", "github.com"] if base == "github.com" else [base]
    return [host_or("gitlab.com", org.base_url)]


def collect_network_hosts(db: Session, org: Organization) -> list[NetworkHostResponse]:
    settings = GitOrgSettings.model_validate(db_resolve_org_settings(db, org))
    config_org_id = db_get_connection_org_id(db, org.id)
    own_git_hosts = _git_hosts(org)
    rows: list[NetworkHostResponse] = []

    def add(
        host: str,
        source: NetworkHostSource,
        label: str,
        *,
        authenticated: bool = False,
        workflows: list[str] | None = None,
    ) -> None:
        rows.append(
            NetworkHostResponse(
                host=host,
                source=source,
                label=label,
                authenticated=authenticated,
                workflows=workflows,
            )
        )

    for host in settings.network.extra_hosts:
        add(host, NetworkHostSource.ORG, "Added by the organization")

    credentials = db_get_credentials_by_org(db, config_org_id)

    for install in db_get_installations_by_org(db, config_org_id):
        marketplace = db_get_marketplace_by_id(db, install.marketplace_id)
        if marketplace:
            host = host_or("", marketplace.git_url)
            if host:
                add(
                    host,
                    NetworkHostSource.SKILL,
                    install.display_name,
                    authenticated=host in own_git_hosts,
                    workflows=install.enabled_workflows or None,
                )
        for cred in credentials:
            if (
                cred.subject_type != SubjectType.PLUGIN_INSTALLATION.value
                or cred.subject_id != install.id
            ):
                continue
            # Dispatch skips these too: oauth2 isn't wired for skills yet.
            settings_host = (cred.settings or {}).get("host")
            if not settings_host or cred.auth_type == AuthType.OAUTH2.value:
                continue
            add(
                host_or(settings_host, settings_host),
                NetworkHostSource.SKILL,
                install.display_name,
                authenticated=cred.auth_type != AuthType.NONE.value,
            )

    for server in db_get_mcp_servers_by_org(db, config_org_id):
        server_host = host_or(server.host, server.host)
        server_authenticated = False
        for cred in credentials:
            if cred.subject_type != SubjectType.MCP_SERVER.value or cred.subject_id != server.id:
                continue
            host = host_or(server_host, (cred.settings or {}).get("host"))
            authenticated = cred.auth_type != AuthType.NONE.value
            if host == server_host:
                server_authenticated = server_authenticated or authenticated
            else:
                add(host, NetworkHostSource.MCP_SERVER, server.name, authenticated=authenticated)
        add(
            server_host,
            NetworkHostSource.MCP_SERVER,
            server.name,
            authenticated=server_authenticated,
        )

    provider_label = "GitHub" if org.provider == "github" else "GitLab"
    for host in own_git_hosts:
        add(host, NetworkHostSource.PLATFORM, provider_label, authenticated=True)

    llm_host = default_llm_host(db)
    if llm_host:
        add(llm_host, NetworkHostSource.PLATFORM, "LLM", authenticated=True)

    if org.workspace_id is not None:
        for sentry_org in db_get_orgs_by_workspace(db, org.workspace_id, provider="sentry"):
            host = host_or("sentry.io", sentry_org.base_url)
            for sentry_host in SENTRY_SAAS_HOSTS if host == "sentry.io" else (host,):
                add(
                    sentry_host,
                    NetworkHostSource.PLATFORM,
                    f"Sentry ({sentry_org.name})",
                    authenticated=True,
                    workflows=[ExecutionWorkflow.FIX.value],
                )

    backend_url = get_current_app().options.backend_url
    if backend_url and org.workspace_id is not None:
        add(
            host_or("localhost", backend_url),
            NetworkHostSource.PLATFORM,
            "Agent memory",
            authenticated=True,
        )

    for host in AGENT_TOOLING_HOSTS:
        add(host, NetworkHostSource.BUILTIN, "Agent tooling")
    for host in BROWSER_TOOLING_HOSTS:
        add(host, NetworkHostSource.BUILTIN, "Browser install", workflows=_BROWSER_WORKFLOWS)
    if settings.demo_videos:
        for host in DEMO_TOOLING_HOSTS:
            add(host, NetworkHostSource.BUILTIN, "Demo app install", workflows=_BROWSER_WORKFLOWS)

    unique = {(r.host, r.source, r.label): r for r in rows}
    return sorted(unique.values(), key=lambda r: (_SOURCE_ORDER.index(r.source), r.host, r.label))
