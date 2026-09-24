"""Tests for shared dispatch credential resolvers."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from api.plugins.container.dispatch_inputs import (
    DispatchInputs,
    add_connectors_to_inputs,
    add_gitlab_workspace_credentials,
    add_memory_to_inputs,
    add_plugin_marketplace_credentials,
    resolve_memory_workspace_id,
)
from api.plugins.container.security_proxy import CredentialKey, build_proxy_spec

_ORG_BY_ID = "api.database.organization.db_get_org_by_id"
_ORGS_BY_WS = "api.database.organization.db_get_orgs_by_workspace"
_REPOS_BY_ORG = "api.database.repository.db_get_repositories_by_org"
_RELATED_REPOS_BULK = "api.database.repository.db_get_related_repos_bulk"
_DESCENDANT_ORGS = "api.database.organization.db_get_descendant_orgs"
_DISPATCH_GIT_TOKEN = "api.plugins.container.utils._get_dispatch_git_token"


def _make_org(
    *,
    provider: str = "gitlab",
    external_org_id: str = "company/backend",
    workspace_id=None,
    auth_token_encrypted: str | None = "enc-tok",
    base_url: str | None = None,
    parent_org_id=None,
) -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    org.provider = provider
    org.external_org_id = external_org_id
    org.workspace_id = workspace_id
    org.auth_token_encrypted = auth_token_encrypted
    org.base_url = base_url
    # Explicit default (rather than leaving the MagicMock auto-attribute,
    # which is a truthy non-None object) so an org with no real hierarchy
    # relation never accidentally forms an edge in
    # ``_connected_org_component``'s ``parent_org_id in by_id`` check.
    org.parent_org_id = parent_org_id
    return org


def _make_app() -> MagicMock:
    db = MagicMock()
    db.session.return_value.__enter__ = MagicMock(return_value=MagicMock())
    db.session.return_value.__exit__ = MagicMock(return_value=False)
    db.decrypt.side_effect = lambda enc: f"decrypted-{enc}"
    app = MagicMock()
    app.database = db
    return app


@pytest.mark.asyncio
async def test_unrelated_org_in_same_workspace_is_excluded() -> None:
    """An org sharing only a workspace_id — no hierarchy or repo-link relation
    to the trigger org — must not be pulled into the dispatch.

    Regression test for the incident that prompted this scoping: an MR on
    one team's repo pulled in 80+ repos from a completely unrelated team's
    GitLab group that happened to share a workspace_id (same tenant account
    connected both), bloating the dispatch secret past Kubernetes' 1 MiB cap.
    """
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="company/backend", workspace_id=ws_id)
    unrelated_org = _make_org(
        external_org_id="other-team/infra",
        workspace_id=ws_id,
        auth_token_encrypted="enc-unrelated",
    )
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, unrelated_org]),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert prefixes == {"/company/backend/"}


@pytest.mark.asyncio
async def test_org_related_via_hierarchy_is_included() -> None:
    """A subgroup in the trigger org's own connected parent_org_id component
    still gets bundled — unlike a same-workspace-but-unrelated org, this one
    shares a real GitLab group tree with the trigger org."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="groupA", workspace_id=ws_id)
    child_org = _make_org(
        external_org_id="groupA/mobile",
        workspace_id=ws_id,
        auth_token_encrypted="enc-child",
        parent_org_id=trigger_org.id,
    )
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, child_org]),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    spec = build_proxy_spec(
        secret_env=inputs.secrets,
        upstreams=inputs.upstreams,
        image="img",
        execution_id="exec-1",
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    # 2 (host, prefix) pairs: each coalesces PRIVATE-TOKEN + Authorization into one entry
    assert len(upstreams) == 2
    prefixes = {u["path_prefix"] for u in upstreams}
    assert prefixes == {"/groupA/", "/groupA/mobile/"}
    # Inject values must use ${VAR} placeholders, not inline secrets
    for entry in upstreams:
        for val in entry["inject"].values():
            assert "${" in val


@pytest.mark.asyncio
async def test_org_related_via_explicit_repo_link_is_included() -> None:
    """A repo explicitly linked (``db_link_repos`` / ``RepositoryMapping``) to
    one of the trigger org's own repos gets credentialed too — ADR-003's
    "related-repo groups may span groups" — even though its owning org has
    no hierarchy relation and shares only the workspace_id. Narrowly: just
    that repo's own path + numeric id, not its owning org's namespace (see
    the next test for why)."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="company/backend", workspace_id=ws_id)
    linked_org = _make_org(
        external_org_id="design-team/assets",
        workspace_id=ws_id,
        auth_token_encrypted="enc-linked",
    )
    app = _make_app()

    trigger_repo = MagicMock()
    trigger_repo.id = uuid4()
    trigger_repo.name = "company/backend/service"
    trigger_repo.external_id = None

    linked_repo = MagicMock()
    linked_repo.id = uuid4()
    linked_repo.name = "design-team/assets/icons"
    linked_repo.external_id = "77"
    linked_repo.org_id = linked_org.id
    linked_repo.auth_token_encrypted = None
    linked_repo.provider_url = None

    def fake_repos_by_org(_db, org_id):
        return [trigger_repo] if org_id == trigger_org.id else []

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, linked_org]),
        patch(_REPOS_BY_ORG, side_effect=fake_repos_by_org),
        patch(_RELATED_REPOS_BULK, return_value={trigger_repo.id: [linked_repo]}),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert prefixes == {
        "/company/backend/",
        "/design-team/assets/icons/",
        "/api/v4/projects/77/",
    }
    # The org's own namespace was never pulled in — only its one linked repo.
    assert "/design-team/assets/" not in prefixes


@pytest.mark.asyncio
async def test_linked_repos_owning_org_other_repos_are_not_pulled_in() -> None:
    """Regression test for the exact production break this scoping caused:
    a repo linked to one repo owned by a company-wide root org (hundreds of
    unrelated repos under one org row, its own token) must not pull in that
    root org's namespace or any of its *other* repos — only the one repo
    actually linked. An earlier version of the scoping fix imported the
    linked org's whole connected component, silently reintroducing the same
    blast radius as the original incident the moment a link touched a big
    root org."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="team-platform", workspace_id=ws_id)
    root_org = _make_org(
        external_org_id="examplecorp",
        workspace_id=ws_id,
        auth_token_encrypted="enc-root",
    )
    app = _make_app()

    trigger_repo = MagicMock()
    trigger_repo.id = uuid4()
    trigger_repo.name = "team-platform/atlas/ingester"
    trigger_repo.external_id = None

    linked_repo = MagicMock()
    linked_repo.id = uuid4()
    linked_repo.name = "orders-api2"
    linked_repo.external_id = "42"
    linked_repo.org_id = root_org.id
    linked_repo.auth_token_encrypted = None
    linked_repo.provider_url = None

    def fake_repos_by_org(_db, org_id):
        # The root org owns hundreds of other repos in reality — none of
        # them should ever be asked for here, since only the one linked
        # repo is in scope, not the whole org.
        return [trigger_repo] if org_id == trigger_org.id else []

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, root_org]),
        patch(_REPOS_BY_ORG, side_effect=fake_repos_by_org),
        patch(_RELATED_REPOS_BULK, return_value={trigger_repo.id: [linked_repo]}),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert prefixes == {
        "/team-platform/",
        "/orders-api2/",
        "/api/v4/projects/42/",
    }
    assert "/examplecorp/" not in prefixes


@pytest.mark.asyncio
async def test_no_workspace_id_uses_trigger_org_only() -> None:
    """Org with no workspace_id → single-org credentials still use path_prefix."""
    trigger_org = _make_org(external_org_id="solo/project", workspace_id=None)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert len(inputs.upstreams) == 2
    assert all(u.path_prefix == "/solo/project/" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_cross_workspace_isolation_only_same_workspace_orgs_included() -> None:
    """db_get_orgs_by_workspace is always called with the trigger org's workspace_id."""
    ws_id = uuid4()
    other_ws_id = uuid4()
    trigger_org = _make_org(external_org_id="ws1/repo", workspace_id=ws_id)

    captured: dict = {}

    def fake_get_orgs(db, workspace_id, provider=None, require_token=True):
        captured["workspace_id"] = workspace_id
        return [trigger_org]

    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, side_effect=fake_get_orgs),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert captured["workspace_id"] == ws_id
    assert captured["workspace_id"] != other_ws_id


@pytest.mark.asyncio
async def test_org_with_no_token_is_skipped() -> None:
    """An org with no encrypted token is skipped; other orgs still get credentials."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="ns/a", workspace_id=ws_id)
    tokenless_org = _make_org(external_org_id="ns/b", workspace_id=ws_id, auth_token_encrypted=None)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, tokenless_org]),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    # Only the trigger org's 2 upstreams (tokenless_org has no repos → skipped)
    assert len(inputs.upstreams) == 2
    assert all(u.path_prefix == "/ns/a/" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_workspace_lookup_includes_placeholder_orgs() -> None:
    """db_get_orgs_by_workspace must be called with require_token=False.

    Regression test: a project-token repo's immediate org can itself be a
    tokenless placeholder (e.g. a personal GitLab namespace with no group
    token). If the workspace lookup excludes tokenless orgs, that org's own
    repos are never considered for the repo-level token fallback below, and
    the CLI dispatch silently gets zero GitLab credentials.
    """
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="adsa", workspace_id=ws_id, auth_token_encrypted=None)
    app = _make_app()

    captured: dict = {}

    def fake_get_orgs(db, workspace_id, provider=None, require_token=True):
        captured["require_token"] = require_token
        return [trigger_org]

    repo = MagicMock()
    repo.name = "jdoe/orders-api"
    repo.auth_token_encrypted = "enc-repo-tok"
    repo.provider_url = None
    repo.external_id = None

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, side_effect=fake_get_orgs),
        patch(_REPOS_BY_ORG, return_value=[repo]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert captured["require_token"] is False
    # The placeholder org's own repo token must still surface as credentials.
    assert len(inputs.upstreams) == 2
    assert all(u.path_prefix == "/jdoe/orders-api/" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_repo_level_token_also_gets_numeric_id_prefix_entry() -> None:
    """Regression test for the exact production break: `glab api -R <repo>
    "projects/:id/..."` resolves `:id` to the numeric GitLab project id
    before issuing the real request, which then carries no namespace for
    the slug-keyed prefix to match. `Repository.external_id` is already
    that numeric id (set at sync/connect time), so it should produce a
    second, numeric-id-keyed upstream entry using the same repo token.
    """
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="adsa", workspace_id=ws_id, auth_token_encrypted=None)
    app = _make_app()

    repo = MagicMock()
    repo.name = "jdoe/webshop"
    repo.auth_token_encrypted = "enc-repo-tok"
    repo.provider_url = None
    repo.external_id = "252"

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org]),
        patch(_REPOS_BY_ORG, return_value=[repo]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert prefixes == {"/jdoe/webshop/", "/api/v4/projects/252/"}
    for prefix in prefixes:
        headers = {u.header for u in inputs.upstreams if u.path_prefix == prefix}
        assert headers == {"PRIVATE-TOKEN", "Authorization"}

    spec = build_proxy_spec(
        secret_env=inputs.secrets, upstreams=inputs.upstreams, image="img", execution_id="exec-1"
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    # Each prefix gets its own ${VAR} secret reference, but every one must
    # resolve to the same underlying repo token.
    tokens = {
        entry["path_prefix"]: inputs.secrets[entry["inject"]["PRIVATE-TOKEN"].strip("${}")]
        for entry in upstreams
        if "PRIVATE-TOKEN" in entry["inject"]
    }
    assert tokens["/jdoe/webshop/"] == tokens["/api/v4/projects/252/"] == "decrypted-enc-repo-tok"


@pytest.mark.asyncio
async def test_repo_without_external_id_gets_no_numeric_id_prefix_entry() -> None:
    """A repo the backend hasn't synced a numeric id for yet degrades to the
    pre-existing behavior — slug-only routing — rather than emitting a
    broken prefix."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="adsa", workspace_id=ws_id, auth_token_encrypted=None)
    app = _make_app()

    repo = MagicMock()
    repo.name = "jdoe/webshop"
    repo.auth_token_encrypted = "enc-repo-tok"
    repo.provider_url = None
    repo.external_id = None

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org]),
        patch(_REPOS_BY_ORG, return_value=[repo]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert len(inputs.upstreams) == 2
    assert all(u.path_prefix == "/jdoe/webshop/" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_org_level_token_gets_per_repo_numeric_id_entries_but_not_redundant_namespace_ones() -> (
    None
):
    """Org-token path: the org's own namespace prefix (``/company/backend/``)
    already covers every repo under it via the proxy's longest-prefix
    ``startswith`` match, so a per-repo namespace entry with the identical
    token would add no coverage — only the numeric-id entry (which can't be
    expressed at the org level) is emitted per repo."""
    trigger_org = _make_org(external_org_id="company/backend", workspace_id=None)
    app = _make_app()

    repo_a = MagicMock()
    repo_a.name = "company/backend/service-a"
    repo_a.external_id = "10"
    repo_b = MagicMock()
    repo_b.name = "company/backend/service-b"
    repo_b.external_id = "11"

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[repo_a, repo_b]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert prefixes == {
        "/company/backend/",
        "/api/v4/projects/10/",
        "/api/v4/projects/11/",
    }

    spec = build_proxy_spec(
        secret_env=inputs.secrets, upstreams=inputs.upstreams, image="img", execution_id="exec-1"
    )
    upstreams = json.loads(spec.config_json)["upstreams"]
    # Each prefix gets its own ${VAR} secret reference, but every one must
    # resolve to the same underlying org token.
    tokens = {
        entry["path_prefix"]: inputs.secrets[entry["inject"]["PRIVATE-TOKEN"].strip("${}")]
        for entry in upstreams
        if "PRIVATE-TOKEN" in entry["inject"]
    }
    assert len({*tokens.values()}) == 1
    assert next(iter(tokens.values())) == "decrypted-enc-tok"
    # All 3 prefixes must reference the exact same secret key, not 3
    # separate keys each duplicating the identical token.
    secret_keys = {entry["inject"]["PRIVATE-TOKEN"] for entry in upstreams}
    assert len(secret_keys) == 1
    assert len(inputs.secrets) == 2  # one PRIVATE-TOKEN key + one git-auth key, total


@pytest.mark.asyncio
async def test_many_repos_same_org_token_writes_secret_once_not_per_repo() -> None:
    """Regression test for the exact incident mechanism: hundreds of repos
    under one org-tokened root group must not multiply the k8s Secret's byte
    count — only the number of *distinct* upstream path-prefix rules (cheap,
    placeholder-only, lives in the ConfigMap) should scale with repo count,
    never the Secret itself (capped at 1 MiB), which must stay at exactly
    one token + one git-auth key regardless of how many repos share it."""
    trigger_org = _make_org(external_org_id="examplecorp", workspace_id=None)
    app = _make_app()

    repos = []
    for i in range(300):
        repo = MagicMock()
        repo.name = f"examplecorp/service-{i}"
        repo.external_id = str(i)
        repos.append(repo)

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=repos),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    # 1 namespace prefix + 300 numeric-id prefixes = 301 upstream rules...
    assert len(inputs.upstreams) == 301 * 2  # PRIVATE-TOKEN + Authorization each
    # ...but the Secret itself holds the token exactly once.
    assert len(inputs.secrets) == 2


@pytest.mark.asyncio
async def test_numeric_external_org_id_still_yields_namespace_prefixes() -> None:
    """GitLab orgs store the numeric group id in ``external_org_id``, so a
    prefix built from it (``/1114/``) matches no request the agent ever makes.
    The namespace prefixes have to come from the repos instead, or git smart
    HTTP and slug-addressed API calls go out with no credential at all."""
    trigger_org = _make_org(external_org_id="1114", workspace_id=None)
    app = _make_app()

    repo = MagicMock()
    repo.name = "team-platform/orders-api"
    repo.external_id = "7831"

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[repo]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert "/1114/" not in prefixes
    assert prefixes == {
        "/team-platform/orders-api/",
        "/api/v4/projects/7831/",
    }


@pytest.mark.asyncio
async def test_org_token_covers_repos_of_tokenless_descendant_subgroups() -> None:
    """A group token covers its subgroups on GitLab's side, and connecting it
    demotes any subgroup token to a placeholder — so the subgroup's repos have
    to be routed through the ancestor's token."""
    ws_id = uuid4()
    trigger_org = _make_org(external_org_id="1114", workspace_id=ws_id)
    subgroup = _make_org(external_org_id="7167", workspace_id=ws_id, auth_token_encrypted=None)

    parent_repo = MagicMock()
    parent_repo.name = "team-platform/orders-api"
    parent_repo.external_id = "7831"
    sub_repo = MagicMock()
    sub_repo.name = "team-platform/atlas/conductor"
    sub_repo.external_id = "9001"

    repos_by_org = {trigger_org.id: [parent_repo], subgroup.id: [sub_repo]}
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, subgroup]),
        patch(_DESCENDANT_ORGS, return_value=[subgroup]),
        patch(_REPOS_BY_ORG, side_effect=lambda _db, org_id: repos_by_org.get(org_id, [])),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    prefixes = {u.path_prefix for u in inputs.upstreams}
    assert "/team-platform/atlas/conductor/" in prefixes
    assert "/api/v4/projects/9001/" in prefixes


@pytest.mark.asyncio
async def test_plugin_marketplace_gets_the_org_token_scoped_to_its_own_path() -> None:
    """A marketplace has no token of its own, and it usually lives outside the
    org's namespace — so none of the per-repo prefixes cover it. Without its
    own entry the CLI clones it with whatever ``extra_hosts`` lets through,
    which for an ``internal`` GitLab repo is nothing."""
    org = _make_org(external_org_id="1114")
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=org),
        patch(_DISPATCH_GIT_TOKEN, return_value="grp-token"),
    ):
        inputs = DispatchInputs()
        await add_plugin_marketplace_credentials(
            inputs,
            git_org_id=org.id,
            git_urls=["https://gitlab.example.org/examplecorp/platform-guild/skills.git"],
        )

    assert {u.path_prefix for u in inputs.upstreams} == {"/examplecorp/platform-guild/skills/"}
    assert {u.host for u in inputs.upstreams} == {"gitlab.example.org"}
    headers = {u.header for u in inputs.upstreams}
    assert headers == {"Authorization", "PRIVATE-TOKEN"}
    basic = next(
        inputs.secrets[u.secret_key] for u in inputs.upstreams if u.header == "Authorization"
    )
    assert basic == "Basic " + base64.b64encode(b"oauth2:grp-token").decode()


@pytest.mark.asyncio
async def test_plugin_marketplace_creds_skipped_without_a_token() -> None:
    """No token means no upstream — a bare allowlist entry would forward the
    clone unauthenticated, which fails as an unhelpful 401."""
    org = _make_org(external_org_id="1114", auth_token_encrypted=None)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=org),
        patch(_DISPATCH_GIT_TOKEN, return_value=None),
    ):
        inputs = DispatchInputs()
        await add_plugin_marketplace_credentials(
            inputs,
            git_org_id=org.id,
            git_urls=["https://gitlab.example.org/examplecorp/platform-guild/skills"],
        )

    assert inputs.upstreams == []
    assert inputs.secrets == {}


@pytest.mark.asyncio
async def test_gitlab_token_placeholder_set_in_public_env() -> None:
    """GITLAB_TOKEN must appear in public_env so glab CLI doesn't bail before making requests.

    glab exits with "Unauthenticated." when GITLAB_TOKEN is absent — the proxy
    never gets to inject the real token. The value is a dummy; the proxy strips
    whatever glab sends and injects the real token from the sidecar.
    """
    trigger_org = _make_org(external_org_id="ns/a", workspace_id=None)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert inputs.public_env.get("GITLAB_TOKEN") == "proxy-injected"
    # Proxy upstreams are unchanged — injection still goes through GITLAB_TOKEN_0
    assert all(u.path_prefix is not None for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_non_gitlab_org_returns_early() -> None:
    """Calling with a non-GitLab org ID is a no-op (wrong provider)."""
    github_org = _make_org(provider="github", external_org_id="acme")
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=github_org),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=github_org.id)

    assert inputs.upstreams == []
    assert inputs.secrets == {}


@pytest.mark.asyncio
async def test_missing_trigger_org_returns_early() -> None:
    """Missing trigger org is a no-op — nothing gets added to inputs."""
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=None),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=uuid4())

    assert inputs.upstreams == []


@pytest.mark.asyncio
async def test_base_url_sets_gitlab_url_env() -> None:
    """A non-default GitLab base_url is propagated as GITLAB_URL."""
    trigger_org = _make_org(
        external_org_id="company/repo",
        workspace_id=None,
        base_url="https://gitlab.mycompany.com",
    )
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert inputs.public_env.get("GITLAB_URL") == "https://gitlab.mycompany.com"
    assert inputs.public_env.get("GITLAB_HOST") == "gitlab.mycompany.com"
    assert all(u.host == "gitlab.mycompany.com" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_base_url_with_port_keeps_port_in_gitlab_host_env() -> None:
    """A self-hosted base_url with a port must not lose it in GITLAB_HOST.

    ``glab`` connects to whatever GITLAB_HOST says verbatim, so a stripped
    port silently redirects it to the default 443 on that host. The proxy
    allowlist (``UpstreamCredential.host``) matches on hostname only, so it
    stays port-less.
    """
    trigger_org = _make_org(
        external_org_id="company/repo",
        workspace_id=None,
        base_url="https://gitlab.mycompany.com:8443",
    )
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    assert inputs.public_env.get("GITLAB_URL") == "https://gitlab.mycompany.com:8443"
    assert inputs.public_env.get("GITLAB_HOST") == "gitlab.mycompany.com:8443"
    assert all(u.host == "gitlab.mycompany.com" for u in inputs.upstreams)


@pytest.mark.asyncio
async def test_workspace_org_with_no_base_url_falls_back_to_gitlab_com_not_trigger_org_host() -> (
    None
):
    """A workspace org with base_url=None must bind to gitlab.com, not the trigger org's host.

    Regression: `org_base_url or base_url` short-circuits to the trigger org's
    self-hosted URL when org_base_url is None, misdirecting that org's token.
    """
    ws_id = uuid4()
    trigger_org = _make_org(
        external_org_id="company/core",
        workspace_id=ws_id,
        base_url="https://gitlab.mycompany.com",
    )
    cloud_org = _make_org(
        external_org_id="oss/lib",
        workspace_id=ws_id,
        auth_token_encrypted="enc-cloud",
        base_url=None,  # cloud GitLab, no base_url
        parent_org_id=trigger_org.id,  # related, so it's still in scope
    )
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=trigger_org),
        patch(_ORGS_BY_WS, return_value=[trigger_org, cloud_org]),
        patch(_REPOS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_gitlab_workspace_credentials(inputs, git_org_id=trigger_org.id)

    hosts = {u.host for u in inputs.upstreams}
    # trigger org → self-hosted; cloud_org → gitlab.com (NOT the trigger org's host)
    assert hosts == {"gitlab.mycompany.com", "gitlab.com"}


# ---------------------------------------------------------------------------
# Memory (#181)
# ---------------------------------------------------------------------------


_SIGNING_SECRET = "api.services.instance_settings.get_or_create_memory_signing_secret"


def _make_memory_app(
    *,
    backend_url: str | None = "https://api.example.com",
    has_database: bool = True,
) -> MagicMock:
    app = MagicMock()
    app.options.backend_url = backend_url
    app.database = _make_app().database if has_database else None
    return app


@pytest.mark.asyncio
async def test_add_memory_to_inputs_wires_secret_upstream_and_public_env() -> None:
    """Token lands in secrets (sidecar-only) + upstream; URL lands in public_env."""
    app = _make_memory_app()
    workspace_id = uuid4()
    execution_id = uuid4()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_SIGNING_SECRET, return_value="unit-test-signing-secret"),
    ):
        inputs = DispatchInputs()
        await add_memory_to_inputs(inputs, workspace_id=workspace_id, execution_id=execution_id)

    token = inputs.secrets[CredentialKey.MEMORY_API_TOKEN]
    assert token  # a real minted token, not a placeholder

    assert len(inputs.upstreams) == 1
    upstream = inputs.upstreams[0]
    assert upstream.secret_key == CredentialKey.MEMORY_API_TOKEN
    assert upstream.host == "api.example.com"
    assert upstream.header == "Authorization"
    assert upstream.bearer is True

    assert inputs.public_env["JEANCLODE_MEMORY_API_URL"] == "https://api.example.com"


@pytest.mark.asyncio
async def test_add_memory_to_inputs_token_never_in_public_env() -> None:
    """The MEMORY_API_TOKEN value must never leak into public_env — sidecar-only,
    same invariant every other credential in this file upholds."""
    app = _make_memory_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_SIGNING_SECRET, return_value="unit-test-signing-secret"),
    ):
        inputs = DispatchInputs()
        await add_memory_to_inputs(inputs, workspace_id=uuid4(), execution_id=uuid4())

    assert CredentialKey.MEMORY_API_TOKEN not in inputs.public_env
    for value in inputs.public_env.values():
        assert value != inputs.secrets[CredentialKey.MEMORY_API_TOKEN]


@pytest.mark.asyncio
async def test_add_memory_to_inputs_skips_when_no_database() -> None:
    """Missing database plugin degrades to a no-op, not a crash."""
    app = _make_memory_app(has_database=False)

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        inputs = DispatchInputs()
        await add_memory_to_inputs(inputs, workspace_id=uuid4(), execution_id=uuid4())

    assert inputs.secrets == {}
    assert inputs.upstreams == []
    assert inputs.public_env == {}


@pytest.mark.asyncio
async def test_add_memory_to_inputs_skips_when_no_backend_url() -> None:
    """Missing backend_url degrades to a no-op, not a crash."""
    app = _make_memory_app(backend_url=None)

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        inputs = DispatchInputs()
        await add_memory_to_inputs(inputs, workspace_id=uuid4(), execution_id=uuid4())

    assert inputs.secrets == {}
    assert inputs.upstreams == []
    assert inputs.public_env == {}


@pytest.mark.asyncio
async def test_add_memory_to_inputs_tokens_differ_per_execution() -> None:
    """Two executions for the same workspace mint distinct tokens (different
    execution_id claims, so an old execution's token can't be replayed for a
    new one even within the same workspace)."""
    app = _make_memory_app()
    workspace_id = uuid4()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_SIGNING_SECRET, return_value="unit-test-signing-secret"),
    ):
        inputs_a = DispatchInputs()
        await add_memory_to_inputs(inputs_a, workspace_id=workspace_id, execution_id=uuid4())
        inputs_b = DispatchInputs()
        await add_memory_to_inputs(inputs_b, workspace_id=workspace_id, execution_id=uuid4())

    assert (
        inputs_a.secrets[CredentialKey.MEMORY_API_TOKEN]
        != inputs_b.secrets[CredentialKey.MEMORY_API_TOKEN]
    )


@pytest.mark.asyncio
async def test_resolve_memory_workspace_id_none_org_id_short_circuits() -> None:
    """No org_id → None, with zero DB access (no app needed at all)."""
    assert await resolve_memory_workspace_id(None) is None


@pytest.mark.asyncio
async def test_resolve_memory_workspace_id_missing_org_returns_none() -> None:
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=None),
    ):
        assert await resolve_memory_workspace_id(uuid4()) is None


@pytest.mark.asyncio
async def test_resolve_memory_workspace_id_org_without_workspace_returns_none() -> None:
    org = _make_org(workspace_id=None)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=org),
    ):
        assert await resolve_memory_workspace_id(org.id) is None


@pytest.mark.asyncio
async def test_resolve_memory_workspace_id_org_with_workspace_returns_its_id() -> None:
    """Memory is always on — any org with a workspace resolves to it, no opt-in."""
    ws_id = uuid4()
    org = _make_org(workspace_id=ws_id)
    app = _make_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_ORG_BY_ID, return_value=org),
    ):
        assert await resolve_memory_workspace_id(org.id) == ws_id


# ---------------------------------------------------------------------------
# add_connectors_to_inputs (#191)
# ---------------------------------------------------------------------------

_MCP_SERVERS_BY_ORG = "api.plugins.container.dispatch_inputs.db_get_mcp_servers_by_org"
_CREDS_BY_ORG = "api.plugins.container.dispatch_inputs.db_get_credentials_by_org"
_INSTALLS_BY_ORG = "api.plugins.container.dispatch_inputs.db_get_installations_by_org"


def _make_mcp_server(
    *, name: str = "outline", host: str = "https://mcp.outline.com/sse"
) -> MagicMock:
    server = MagicMock()
    server.id = uuid4()
    server.name = name
    server.host = host
    return server


def _make_install(*, org_id=None) -> MagicMock:
    install = MagicMock()
    install.id = uuid4()
    install.org_id = org_id or uuid4()
    return install


def _make_credential(
    *,
    subject_type: str,
    subject_id,
    auth_type: str,
    settings: dict,
    secret: dict,
) -> MagicMock:
    cred = MagicMock()
    cred.id = uuid4()
    cred.subject_type = subject_type
    cred.subject_id = subject_id
    cred.auth_type = auth_type
    cred.settings = settings
    # decrypt() is mocked to json.loads the "encrypted" payload directly.
    cred.secret_encrypted = json.dumps(secret)
    return cred


def _make_connectors_app(decrypt_passthrough: bool = True) -> MagicMock:
    app = _make_app()
    if decrypt_passthrough:
        app.database.decrypt.side_effect = lambda enc: enc
    return app


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_with_api_key_wires_upstream_and_payload() -> None:
    org_id = uuid4()
    server = _make_mcp_server()
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="api_key",
        settings={"header": "Authorization", "value_prefix": "Bearer "},
        secret={"name": "unused", "key": "sk-outline-123"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert len(inputs.upstreams) == 1
    upstream = inputs.upstreams[0]
    assert upstream.host == "mcp.outline.com"
    assert upstream.header == "Authorization"
    assert upstream.bearer is True
    assert inputs.secrets[upstream.secret_key] == "sk-outline-123"

    payload = json.loads(inputs.public_env["JEANCLODE_MCP_SERVERS"])
    assert payload == [
        {
            "name": "outline",
            "url": "https://mcp.outline.com/sse",
            "header": "Authorization",
            "auth_scheme": "bearer",
        }
    ]


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_with_oauth2_wires_oauth_upstream() -> None:
    org_id = uuid4()
    server = _make_mcp_server(name="figma-like-thing", host="https://mcp.example.com/mcp")
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="oauth2",
        settings={
            "token_url": "https://auth.example.com/token",
            "grant_type": "client_credentials",
        },
        secret={"client_id": "cid-123", "client_secret": "csec-456"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.upstreams == []
    assert len(inputs.oauth_upstreams) == 1
    oauth = inputs.oauth_upstreams[0]
    assert oauth.host == "mcp.example.com"
    assert oauth.token_url == "https://auth.example.com/token"
    assert inputs.secrets[oauth.client_id_key] == "cid-123"
    assert inputs.secrets[oauth.client_secret_key] == "csec-456"

    payload = json.loads(inputs.public_env["JEANCLODE_MCP_SERVERS"])
    assert payload[0]["auth_scheme"] == "bearer"
    assert payload[0]["header"] == "Authorization"


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_with_oauth2_password_grant_wires_username_password() -> (
    None
):
    org_id = uuid4()
    server = _make_mcp_server(name="legacy-api", host="https://mcp.example.com/mcp")
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="oauth2",
        settings={"token_url": "https://auth.example.com/token", "grant_type": "password"},
        secret={
            "client_id": "cid-123",
            "client_secret": "csec-456",
            "username": "bob",
            "password": "pw",
        },
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    oauth = inputs.oauth_upstreams[0]
    assert oauth.grant_type == "password"
    assert inputs.secrets[oauth.username_key] == "bob"
    assert inputs.secrets[oauth.password_key] == "pw"
    assert oauth.refresh_token_key is None


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_with_oauth2_refresh_token_grant_wires_refresh_token() -> (
    None
):
    org_id = uuid4()
    server = _make_mcp_server(name="legacy-api", host="https://mcp.example.com/mcp")
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="oauth2",
        settings={"token_url": "https://auth.example.com/token", "grant_type": "refresh_token"},
        secret={"client_id": "cid-123", "client_secret": "csec-456", "refresh_token": "rt-1"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    oauth = inputs.oauth_upstreams[0]
    assert oauth.grant_type == "refresh_token"
    assert inputs.secrets[oauth.refresh_token_key] == "rt-1"
    assert oauth.username_key is None
    assert oauth.password_key is None


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_without_credential_is_unauthenticated_entry() -> None:
    org_id = uuid4()
    server = _make_mcp_server()
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.upstreams == []
    assert inputs.oauth_upstreams == []
    payload = json.loads(inputs.public_env["JEANCLODE_MCP_SERVERS"])
    assert payload == [{"name": "outline", "url": "https://mcp.outline.com/sse"}]


@pytest.mark.asyncio
async def test_add_connectors_skill_api_key_uses_secret_name_as_sidecar_key() -> None:
    """The skill's own env var name doubles as the sidecar secret_key —
    the agent gets its placeholder for free via the existing per-secret
    placeholder mechanism, no separate public_env write needed."""
    org_id = uuid4()
    install = _make_install(org_id=org_id)
    cred = _make_credential(
        subject_type="plugin_installation",
        subject_id=install.id,
        auth_type="api_key",
        settings={
            "header": "Authorization",
            "value_prefix": "Bearer ",
            "host": "api.outline.internal",
        },
        secret={"name": "OUTLINE_API_KEY", "key": "sk-outline-999"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[install]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.secrets["OUTLINE_API_KEY"] == "sk-outline-999"
    assert "JEANCLODE_MCP_SERVERS" not in inputs.public_env
    # No explicit placeholder write — kubernetes.py's per-secret-key
    # mechanism (list(request.secrets.keys())) covers it.
    assert "OUTLINE_API_KEY" not in inputs.public_env
    upstream = inputs.upstreams[0]
    assert upstream.secret_key == "OUTLINE_API_KEY"
    assert upstream.host == "api.outline.internal"
    assert upstream.bearer is True


async def _connectors(creds: list, *, installs: list | None = None, servers: list | None = None):
    app = _make_connectors_app()
    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=servers or []),
        patch(_CREDS_BY_ORG, return_value=creds),
        patch(_INSTALLS_BY_ORG, return_value=installs or []),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=uuid4())
    return inputs


@pytest.mark.asyncio
async def test_add_connectors_skill_basic_auth_without_name_gets_a_generated_key() -> None:
    install = _make_install()
    cred = _make_credential(
        subject_type="plugin_installation",
        subject_id=install.id,
        auth_type="basic_auth",
        settings={"host": "api.internal"},
        secret={"username": "u", "password": "p"},
    )

    inputs = await _connectors([cred], installs=[install])

    (key,) = inputs.secrets
    assert key.startswith("SKILL_AUTH_")
    assert inputs.secrets[key] == "Basic dTpw"
    assert inputs.upstreams[0].host == "api.internal"
    assert inputs.upstreams[0].secret_key == key


@pytest.mark.asyncio
async def test_add_connectors_skill_basic_auth_uses_its_name_when_given() -> None:
    install = _make_install()
    cred = _make_credential(
        subject_type="plugin_installation",
        subject_id=install.id,
        auth_type="basic_auth",
        settings={"host": "api.internal"},
        secret={"name": "ZONING_AUTH", "username": "u", "password": "p"},
    )

    inputs = await _connectors([cred], installs=[install])

    assert list(inputs.secrets) == ["ZONING_AUTH"]


@pytest.mark.asyncio
async def test_add_connectors_skill_none_only_allowlists_the_host() -> None:
    install = _make_install()
    cred = _make_credential(
        subject_type="plugin_installation",
        subject_id=install.id,
        auth_type="none",
        settings={"host": "*.s3.us-west-2.amazonaws.com"},
        secret={},
    )

    inputs = await _connectors([cred], installs=[install])

    assert inputs.extra_hosts == ["*.s3.us-west-2.amazonaws.com"]
    assert inputs.secrets == {}
    assert inputs.upstreams == []


@pytest.mark.asyncio
async def test_add_connectors_skill_with_several_auths_wires_each_one() -> None:
    install = _make_install()
    creds = [
        _make_credential(
            subject_type="plugin_installation",
            subject_id=install.id,
            auth_type="api_key",
            settings={"host": "api.figma.com", "header": "X-Figma-Token", "value_prefix": None},
            secret={"name": "FIGMA_API_TOKEN", "key": "figd_1"},
        ),
        _make_credential(
            subject_type="plugin_installation",
            subject_id=install.id,
            auth_type="none",
            settings={"host": "figma-alpha-api.s3.us-west-2.amazonaws.com"},
            secret={},
        ),
    ]

    inputs = await _connectors(creds, installs=[install])

    assert inputs.secrets == {"FIGMA_API_TOKEN": "figd_1"}
    assert [u.host for u in inputs.upstreams] == ["api.figma.com"]
    assert inputs.extra_hosts == ["figma-alpha-api.s3.us-west-2.amazonaws.com"]


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_extra_auth_targets_its_own_host() -> None:
    server = _make_mcp_server()
    creds = [
        _make_credential(
            subject_type="mcp_server",
            subject_id=server.id,
            auth_type="api_key",
            settings={"header": "Authorization", "value_prefix": "Bearer "},
            secret={"name": "x", "key": "sk-server"},
        ),
        _make_credential(
            subject_type="mcp_server",
            subject_id=server.id,
            auth_type="api_key",
            settings={"host": "api.tools.example", "header": "X-Key", "value_prefix": None},
            secret={"name": "y", "key": "sk-tools"},
        ),
    ]

    inputs = await _connectors(creds, servers=[server])

    by_host = {u.host: u for u in inputs.upstreams}
    assert by_host["mcp.outline.com"].path_prefix == "/sse"
    assert by_host["api.tools.example"].path_prefix is None
    assert by_host["api.tools.example"].header == "X-Key"
    assert inputs.secrets == {"MCP_0": "sk-server", "MCP_0_1": "sk-tools"}
    payload = json.loads(inputs.public_env["JEANCLODE_MCP_SERVERS"])
    assert payload[0]["header"] == "Authorization"


@pytest.mark.asyncio
async def test_add_connectors_skill_missing_settings_host_is_skipped() -> None:
    org_id = uuid4()
    install = _make_install(org_id=org_id)
    cred = _make_credential(
        subject_type="plugin_installation",
        subject_id=install.id,
        auth_type="api_key",
        settings={},  # no host
        secret={"name": "X_API_KEY", "key": "sk-x"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[install]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.secrets == {}
    assert inputs.upstreams == []


@pytest.mark.asyncio
async def test_add_connectors_noop_when_no_mcp_servers_or_credentials() -> None:
    org_id = uuid4()
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[]),
        patch(_CREDS_BY_ORG, return_value=[]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.secrets == {}
    assert inputs.upstreams == []
    assert inputs.public_env == {}


@pytest.mark.asyncio
async def test_add_connectors_two_mcp_servers_sharing_host_get_distinct_path_prefixes() -> None:
    """Regression test: two MCP servers behind one gateway, differentiated
    only by URL path, must not collide in the proxy's host-only matching —
    each needs its own path_prefix derived from its URL."""
    org_id = uuid4()
    server_a = _make_mcp_server(name="tool-a", host="https://gateway.acme.com/tool-a")
    server_b = _make_mcp_server(name="tool-b", host="https://gateway.acme.com/tool-b")
    cred_a = _make_credential(
        subject_type="mcp_server",
        subject_id=server_a.id,
        auth_type="api_key",
        settings={},
        secret={"name": "unused", "key": "key-a"},
    )
    cred_b = _make_credential(
        subject_type="mcp_server",
        subject_id=server_b.id,
        auth_type="api_key",
        settings={},
        secret={"name": "unused", "key": "key-b"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server_a, server_b]),
        patch(_CREDS_BY_ORG, return_value=[cred_a, cred_b]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert len(inputs.upstreams) == 2
    by_prefix = {u.path_prefix: u for u in inputs.upstreams}
    assert set(by_prefix) == {"/tool-a", "/tool-b"}
    assert all(u.host == "gateway.acme.com" for u in inputs.upstreams)
    assert inputs.secrets[by_prefix["/tool-a"].secret_key] == "key-a"
    assert inputs.secrets[by_prefix["/tool-b"].secret_key] == "key-b"


@pytest.mark.asyncio
async def test_add_connectors_mcp_server_at_root_path_has_no_path_prefix() -> None:
    org_id = uuid4()
    server = _make_mcp_server(host="https://mcp.outline.com/")
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="api_key",
        settings={},
        secret={"name": "unused", "key": "sk"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG, return_value=[]),
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    assert inputs.upstreams[0].path_prefix is None


@pytest.mark.asyncio
async def test_add_connectors_skips_installations_query_when_no_skill_credentials() -> None:
    """Perf regression test: the installations lookup is only paid for when
    a credential actually targets a skill — MCP-only orgs (the common
    case) must not trigger it."""
    org_id = uuid4()
    server = _make_mcp_server()
    cred = _make_credential(
        subject_type="mcp_server",
        subject_id=server.id,
        auth_type="api_key",
        settings={},
        secret={"name": "unused", "key": "sk"},
    )
    app = _make_connectors_app()

    with (
        patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app),
        patch(_MCP_SERVERS_BY_ORG, return_value=[server]),
        patch(_CREDS_BY_ORG, return_value=[cred]),
        patch(_INSTALLS_BY_ORG) as mock_installs,
    ):
        inputs = DispatchInputs()
        await add_connectors_to_inputs(inputs, git_org_id=org_id)

    mock_installs.assert_not_called()


# ---------------------------------------------------------------------------
# add_notify_to_inputs
# ---------------------------------------------------------------------------

_NOTIFY_ORG_BY_ID = "api.database.organization.db_get_org_by_id"
_NOTIFY_SETTINGS = "api.database.organization.db_resolve_org_settings"
_NOTIFY_HANDLES = "api.database.organization.db_resolve_notify_handles"


def _notify_app() -> MagicMock:
    db = MagicMock()
    db.session.return_value.__enter__ = MagicMock(return_value=MagicMock())
    db.session.return_value.__exit__ = MagicMock(return_value=False)
    app = MagicMock()
    app.database = db
    return app


def test_add_notify_to_inputs_serializes_resolved_handles():
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()
    org = _make_org(provider="gitlab")

    with (
        patch(
            "api.plugins.container.dispatch_inputs.get_current_app",
            return_value=_notify_app(),
        ),
        patch(_NOTIFY_ORG_BY_ID, return_value=org),
        patch(_NOTIFY_SETTINGS, return_value={"notify": {"on_ready": [str(uuid4())]}}),
        patch(_NOTIFY_HANDLES, return_value=["alice", "bob"]),
    ):
        add_notify_to_inputs(inputs, git_org_id=org.id)

    assert json.loads(inputs.public_env[NOTIFY_USERS_ENV_VAR]) == ["alice", "bob"]


def test_add_notify_to_inputs_sets_nothing_when_list_is_empty():
    """Absence of the env var is the CLI's off switch, so an unconfigured org
    must not ship an empty array that reads as "configured"."""
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()
    org = _make_org()

    with (
        patch(
            "api.plugins.container.dispatch_inputs.get_current_app",
            return_value=_notify_app(),
        ),
        patch(_NOTIFY_ORG_BY_ID, return_value=org),
        patch(_NOTIFY_SETTINGS, return_value={}),
        patch(_NOTIFY_HANDLES, return_value=[]),
    ):
        add_notify_to_inputs(inputs, git_org_id=org.id)

    assert NOTIFY_USERS_ENV_VAR not in inputs.public_env


def test_add_notify_to_inputs_reads_settings_through_the_org_chain():
    """A list set on a GitLab group has to reach its subgroups' dispatches —
    the subgroup org's own settings are empty."""
    from api.plugins.container.dispatch_inputs import add_notify_to_inputs

    inputs = DispatchInputs()
    org = _make_org()

    with (
        patch(
            "api.plugins.container.dispatch_inputs.get_current_app",
            return_value=_notify_app(),
        ),
        patch(_NOTIFY_ORG_BY_ID, return_value=org),
        patch(_NOTIFY_SETTINGS, return_value={}) as resolve_settings,
        patch(_NOTIFY_HANDLES, return_value=["alice"]),
    ):
        add_notify_to_inputs(inputs, git_org_id=org.id)

    resolve_settings.assert_called_once()


def test_add_notify_to_inputs_swallows_resolution_failure():
    """A broken notify list must not take down a dispatch that would
    otherwise produce a perfectly good MR."""
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()
    org = _make_org()

    with (
        patch(
            "api.plugins.container.dispatch_inputs.get_current_app",
            return_value=_notify_app(),
        ),
        patch(_NOTIFY_ORG_BY_ID, side_effect=RuntimeError("db gone")),
    ):
        add_notify_to_inputs(inputs, git_org_id=org.id)

    assert NOTIFY_USERS_ENV_VAR not in inputs.public_env


def test_add_notify_to_inputs_noops_without_a_database():
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()
    app = MagicMock()
    app.database = None

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        add_notify_to_inputs(inputs, git_org_id=uuid4())

    assert NOTIFY_USERS_ENV_VAR not in inputs.public_env


def test_add_notify_to_inputs_skips_a_missing_org():
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()

    with (
        patch(
            "api.plugins.container.dispatch_inputs.get_current_app",
            return_value=_notify_app(),
        ),
        patch(_NOTIFY_ORG_BY_ID, return_value=None),
    ):
        add_notify_to_inputs(inputs, git_org_id=uuid4())

    assert NOTIFY_USERS_ENV_VAR not in inputs.public_env


def test_add_notify_to_inputs_survives_an_uninitialized_app():
    """The mention-driven launch paths can run without an app context bound;
    a notification must never be what breaks a dispatch."""
    from api.plugins.container.dispatch_inputs import (
        NOTIFY_USERS_ENV_VAR,
        add_notify_to_inputs,
    )

    inputs = DispatchInputs()

    with patch(
        "api.plugins.container.dispatch_inputs.get_current_app",
        side_effect=RuntimeError("Application not initialized"),
    ):
        add_notify_to_inputs(inputs, git_org_id=uuid4())

    assert NOTIFY_USERS_ENV_VAR not in inputs.public_env


def _llm_env_app(*, oauth_token=None, api_key=None, model_high=None, model_low=None) -> MagicMock:
    from options import ClaudeCodeOptions

    app = MagicMock()
    app.options.claude_code = ClaudeCodeOptions(
        oauth_token=oauth_token, api_key=api_key, model_high=model_high, model_low=model_low
    )
    return app


def test_add_llm_to_inputs_env_var_path_sets_model_env_when_configured():
    """The env-var LLM override (CLAUDE_CODE_OAUTH_TOKEN/.env) used to skip
    JEANCLODE_MODEL/JEANCLODE_SMALL_MODEL entirely — only the DB credential
    pool branch set them — so every env-var dispatch silently ran the CLI's
    hardcoded sonnet default instead of the operator's configured tier."""
    from api.plugins.container.dispatch_inputs import add_llm_to_inputs

    inputs = DispatchInputs()
    app = _llm_env_app(oauth_token="oauth-secret", model_high="haiku", model_low="haiku")

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        result = add_llm_to_inputs(inputs)

    assert result.available
    assert inputs.public_env["JEANCLODE_MODEL"] == "haiku"
    assert inputs.public_env["JEANCLODE_SMALL_MODEL"] == "haiku"


def test_add_llm_to_inputs_env_var_path_api_key_sets_model_env_when_configured():
    from api.plugins.container.dispatch_inputs import add_llm_to_inputs

    inputs = DispatchInputs()
    app = _llm_env_app(api_key="sk-ant-secret", model_high="haiku", model_low="haiku")

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        add_llm_to_inputs(inputs)

    assert inputs.public_env["JEANCLODE_MODEL"] == "haiku"
    assert inputs.public_env["JEANCLODE_SMALL_MODEL"] == "haiku"


def test_add_llm_to_inputs_env_var_path_omits_model_env_when_unconfigured():
    """Unset model_high/model_low must not inject empty/placeholder values —
    the CLI's own default (sonnet/haiku) takes over when the key is absent."""
    from api.plugins.container.dispatch_inputs import add_llm_to_inputs

    inputs = DispatchInputs()
    app = _llm_env_app(oauth_token="oauth-secret")

    with patch("api.plugins.container.dispatch_inputs.get_current_app", return_value=app):
        add_llm_to_inputs(inputs)

    assert "JEANCLODE_MODEL" not in inputs.public_env
    assert "JEANCLODE_SMALL_MODEL" not in inputs.public_env
