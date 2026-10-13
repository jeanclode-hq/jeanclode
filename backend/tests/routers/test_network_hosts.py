"""``GET /organizations/{org_id}/network/hosts`` — what a run of an org can reach."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.models.connectors import Credential, McpServer
from api.models.organizations import Organization
from api.models.plugins import PluginInstallation, PluginMarketplace
from api.models.workspaces import Workspace
from api.routers.organizations.network import collect_network_hosts
from api.routers.organizations.route import get_organization_network_hosts


def _app(backend_url: str | None = "https://jeanclode.example.com") -> MagicMock:
    app = MagicMock()
    app.options.backend_url = backend_url
    app.options.claude_code.oauth_token = None
    app.options.claude_code.api_key = "sk-test"
    return app


def _collect(db: Session, org: Organization):
    with (
        patch("api.routers.organizations.network.get_current_app", return_value=_app()),
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=_app()),
    ):
        return collect_network_hosts(db, org)


@pytest.fixture
def gitlab_org(db_session: Session) -> Organization:
    workspace = Workspace(name="ws", slug=f"ws-{uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    org = Organization(
        workspace_id=workspace.id,
        name="acme",
        provider="gitlab",
        external_org_id="acme",
        base_url="https://gitlab.acme.dev",
        auth_token_encrypted="enc",
        settings={"network": {"extra_hosts": ["fonts.acme.dev"]}},
    )
    db_session.add(org)
    db_session.commit()
    return org


def _rows(hosts, source: str) -> set[tuple[str, str, bool]]:
    return {(h.host, h.label, h.authenticated) for h in hosts if h.source == source}


def test_lists_the_org_hosts_first(db_session: Session, gitlab_org: Organization) -> None:
    hosts = _collect(db_session, gitlab_org)

    assert hosts[0].host == "fonts.acme.dev"
    assert hosts[0].source == "org"
    assert hosts[0].workflows is None


def test_lists_skill_hosts_with_their_auth(db_session: Session, gitlab_org: Organization) -> None:
    marketplace = PluginMarketplace(
        org_id=gitlab_org.id, name="skills", git_url="https://gitlab.acme.dev/acme/skills"
    )
    db_session.add(marketplace)
    db_session.flush()
    install = PluginInstallation(
        org_id=gitlab_org.id,
        marketplace_id=marketplace.id,
        plugin_name="figma",
        display_name="Figma",
        enabled_workflows=["issue_resolve"],
    )
    db_session.add(install)
    db_session.flush()
    for auth_type, host in [
        ("api_key", "api.figma.com"),
        ("none", "cdn.figma.com"),
        ("oauth2", "oauth.figma.com"),
    ]:
        db_session.add(
            Credential(
                org_id=gitlab_org.id,
                subject_type="plugin_installation",
                subject_id=install.id,
                auth_type=auth_type,
                settings={"host": host},
                secret_encrypted="enc",
            )
        )
    db_session.commit()

    hosts = _collect(db_session, gitlab_org)

    assert _rows(hosts, "skill") == {
        ("gitlab.acme.dev", "Figma", True),
        ("api.figma.com", "Figma", True),
        ("cdn.figma.com", "Figma", False),
    }
    repo_row = next(h for h in hosts if h.source == "skill" and h.host == "gitlab.acme.dev")
    assert repo_row.workflows == ["issue_resolve"]


def test_lists_mcp_server_hosts(db_session: Session, gitlab_org: Organization) -> None:
    server = McpServer(org_id=gitlab_org.id, name="linear", host="https://mcp.linear.app/sse")
    db_session.add(server)
    db_session.flush()
    db_session.add(
        Credential(
            org_id=gitlab_org.id,
            subject_type="mcp_server",
            subject_id=server.id,
            auth_type="api_key",
            settings={},
            secret_encrypted="enc",
        )
    )
    db_session.commit()

    hosts = _collect(db_session, gitlab_org)

    assert _rows(hosts, "mcp_server") == {("mcp.linear.app", "linear", True)}


def test_lists_platform_and_builtin_hosts(db_session: Session, gitlab_org: Organization) -> None:
    hosts = _collect(db_session, gitlab_org)

    platform = _rows(hosts, "platform")
    assert ("gitlab.acme.dev", "GitLab", True) in platform
    assert ("api.anthropic.com", "LLM", True) in platform
    assert ("jeanclode.example.com", "Agent memory", True) in platform
    builtin = {h.host for h in hosts if h.source == "builtin"}
    assert {"github.com", "registry.npmjs.org", "release-assets.githubusercontent.com"} <= builtin


def test_demo_install_hosts_follow_the_demo_switch(
    db_session: Session, gitlab_org: Organization
) -> None:
    gitlab_org.settings = {"demo_videos": False}
    db_session.commit()

    hosts = _collect(db_session, gitlab_org)

    assert not any(h.label == "Demo app install" for h in hosts)


def test_sentry_hosts_only_reach_sentry_fix(db_session: Session, gitlab_org: Organization) -> None:
    db_session.add(
        Organization(
            workspace_id=gitlab_org.workspace_id,
            name="acme-sentry",
            provider="sentry",
            external_org_id="acme-sentry",
            auth_token_encrypted="enc",
        )
    )
    db_session.commit()

    hosts = _collect(db_session, gitlab_org)

    sentry = [h for h in hosts if h.label == "Sentry (acme-sentry)"]
    assert {h.host for h in sentry} == {"sentry.io", "us.sentry.io", "de.sentry.io", "eu.sentry.io"}
    assert all(h.workflows == ["fix"] for h in sentry)


def test_refuses_a_sentry_org() -> None:
    org = MagicMock()
    org.provider = "sentry"
    with (
        patch("api.routers.organizations.route.verify_org_access_from_body"),
        patch("api.routers.organizations.route.db_get_org_by_id", return_value=org),
        pytest.raises(HTTPException) as exc,
    ):
        get_organization_network_hosts(org_id=uuid4(), current_user=MagicMock(), db=MagicMock())

    assert exc.value.status_code == 400
