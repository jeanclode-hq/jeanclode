"""Shared credential resolvers for sandboxed container dispatch.

Every plugin that launches a CLI container (sentry, github, gitlab, …)
needs the same baseline: an LLM credential, the git platform credential
for the org owning the target repo, and the agent-tooling host
allowlist. These resolvers live here so each domain plugin's launch
module only owns the source-specific bits (sentry token; PR command
shape; etc.).
"""

from __future__ import annotations

import base64
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field

from api.context import get_current_app
from api.database.connectors import db_get_credentials_by_org, db_get_mcp_servers_by_org
from api.database.llm_credentials import (
    LLMCredentialAvailability,
    db_list_llm_credentials,
    db_select_llm_credential,
    llm_credential_is_stale,
)
from api.database.plugins import db_get_installations_by_org
from api.models.connectors import AuthType, Credential, SubjectType
from api.models.llm_credentials import LLMCredential
from api.plugins.container.security_proxy import CredentialKey, OAuthUpstream, UpstreamCredential
from api.services.llm_credentials import decrypt_secret
from api.services.memory_token import mint_memory_token

logger = logging.getLogger(__name__)


# Hosts the Claude Code subprocess and skill plugins need to reach
# without auth: plugin manifests, public clones, and the GitHub REST
# endpoint that ``gh repo clone`` consults before falling back to git.
# When the dispatching org also has a ``GH_TOKEN`` resolved by
# ``add_git_platform_to_inputs``, the per-host coalescing in
# ``_serialize_upstreams`` keeps the token-injecting rule intact.
#
# ``codeload.github.com`` handles the 302 that ``gh api repos/.../zipball/<sha>``
# returns on private repos — the redirect URL embeds a short-lived
# signed token, so the proxy only needs to allow the host (no header
# injection). Without this, PR-based workflows fail at the clone step.
AGENT_TOOLING_HOSTS = (
    "raw.githubusercontent.com",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)


class DispatchInputs(BaseModel):
    """Everything a container backend needs to launch one sandboxed pod."""

    public_env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    upstreams: list[UpstreamCredential] = Field(default_factory=list)
    # oauth2 client_credentials rules the sidecar mints and refreshes on
    # its own — see add_connectors_to_inputs.
    oauth_upstreams: list[OAuthUpstream] = Field(default_factory=list)
    # Hosts the agent reaches without auth — public CDNs the Claude Code
    # subprocess and skill plugins fetch from. These contribute only to
    # the proxy allowlist; no header injection happens.
    extra_hosts: list[str] = Field(default_factory=list)


def _url_path_prefix(url: str) -> str | None:
    """The path component of a URL, or ``None`` for a bare host/root path.

    Two subjects sharing a host but differentiated only by path (e.g. two
    MCP servers behind one gateway, ``gateway.acme.com/tool-a`` vs
    ``gateway.acme.com/tool-b``) would otherwise collide in the proxy's
    host-only ``Upstream`` matching — ``host_or`` deliberately discards the
    path when resolving the allowlist host, so callers that need to keep
    two same-host credentials apart must carry the path separately as
    ``path_prefix``. Same pattern as GitLab's per-namespace routing in
    ``add_gitlab_workspace_credentials``.
    """
    path = urlparse(url).path
    return path if path and path != "/" else None


def host_or(default: str, *candidates: str | None) -> str:
    """Return the first parseable host from the candidates, or the default.

    Port-less: used for the security-proxy allowlist, which matches
    ``flow.request.pretty_host`` (hostname only, mitmproxy strips the port
    before this ever sees it). Use ``host_port_or`` for env vars that ``gh``/
    ``glab`` connect to directly, where a non-default port must survive.
    """
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.hostname:
            return parsed.hostname
    return default


def host_port_or(default: str, *candidates: str | None) -> str:
    """Like ``host_or`` but keeps an explicit port (e.g. ``gitlab.internal:8080``).

    ``GH_HOST``/``GITLAB_HOST`` are consumed verbatim by ``gh``/``glab`` as
    the literal connection target — dropping the port there breaks any
    self-hosted instance that isn't on 443.
    """
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.hostname:
            return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    return default


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class LLMSelectionResult(BaseModel):
    """Outcome of walking the LLM credential pool for one dispatch."""

    availability: LLMCredentialAvailability
    retry_at: datetime | None = None

    @property
    def available(self) -> bool:
        return self.availability == LLMCredentialAvailability.AVAILABLE


def add_llm_to_inputs(inputs: DispatchInputs, *, fixer_options: bool = False) -> LLMSelectionResult:
    """Resolve the LLM credential — env-options first, the admin-configured
    credential pool as fallback (ADR-010).

    Priority: env (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) > the
    priority-ordered ``llm_credentials`` pool. Env vars are an operator
    override outside the pool/failover model entirely — always
    ``AVAILABLE`` and never subject to staleness.

    Provider determines which env var name + injection scheme is used:
      * ``claude_code`` → ``CLAUDE_CODE_OAUTH_TOKEN`` (Bearer header)
      * ``anthropic`` → ``ANTHROPIC_API_KEY`` (``x-api-key`` header)
      * ``openai`` / ``openai_compatible`` → ``OPENAI_API_KEY``; host is
        derived from ``base_url`` so self-hosted endpoints work.

    ``fixer_options`` is for issue-resolve, whose triage can move the fixer
    to another credential; nobody else gets the extra credentials' secrets
    or hosts.

    Returns a :class:`LLMSelectionResult` so callers can distinguish real,
    temporary exhaustion (every pool row currently stale — retry later)
    from misconfiguration (an empty pool — fail fast, nothing to wait for).
    """
    app = get_current_app()

    claude_opts = app.options.claude_code
    if claude_opts.oauth_token:
        inputs.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN] = claude_opts.oauth_token
        _set_model_env(inputs, claude_opts.model_high, claude_opts.model_low)
        inputs.upstreams.append(_anthropic_oauth_upstream())
        return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)
    if claude_opts.api_key:
        inputs.secrets[CredentialKey.ANTHROPIC_API_KEY] = claude_opts.api_key
        _set_model_env(inputs, claude_opts.model_high, claude_opts.model_low)
        inputs.upstreams.append(_anthropic_apikey_upstream())
        return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)

    if not app.database:
        return LLMSelectionResult(availability=LLMCredentialAvailability.NONE_CONFIGURED)

    with app.database.session() as db:
        availability, credential, retry_at = db_select_llm_credential(db)
        if availability != LLMCredentialAvailability.AVAILABLE or credential is None:
            return LLMSelectionResult(availability=availability, retry_at=retry_at)
        _add_llm_from_credential(inputs, credential)
        if not fixer_options:
            return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)
        try:
            _add_llm_options(inputs, credential, db_list_llm_credentials(db))
        except Exception:
            # The options only widen the fixer's choice; the run must not
            # lose its primary credential over them.
            logger.warning("skipping fixer LLM options", exc_info=True)

    return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)


def check_llm_availability() -> LLMSelectionResult:
    """Dry-run of :func:`add_llm_to_inputs`'s credential resolution, without
    wiring any :class:`DispatchInputs`.

    Used by dispatch paths that need to know availability *before* deciding
    whether to create an execution at all — Sentry's dispatch tick, so a
    temporarily-exhausted pool never creates an execution row in the first
    place (see ADR-010: leaving the issue untouched is what makes it
    naturally retryable on the next tick, with nothing new to build).
    """
    app = get_current_app()

    claude_opts = app.options.claude_code
    if claude_opts.oauth_token or claude_opts.api_key:
        return LLMSelectionResult(availability=LLMCredentialAvailability.AVAILABLE)

    if not app.database:
        return LLMSelectionResult(availability=LLMCredentialAvailability.NONE_CONFIGURED)

    with app.database.session() as db:
        availability, _, retry_at = db_select_llm_credential(db)
    return LLMSelectionResult(availability=availability, retry_at=retry_at)


def _set_model_env(inputs: DispatchInputs, model_high: str | None, model_low: str | None) -> None:
    """Set JEANCLODE_MODEL / JEANCLODE_SMALL_MODEL when a tier is configured.

    Shared by the env-var override branches and the DB-credential path so
    both ways of resolving an LLM credential can steer which model tier a
    dispatch actually runs — unset leaves the CLI's own default in effect.
    """
    if model_high:
        inputs.public_env["JEANCLODE_MODEL"] = model_high
    if model_low:
        inputs.public_env["JEANCLODE_SMALL_MODEL"] = model_low


def _add_llm_from_credential(inputs: DispatchInputs, credential: LLMCredential) -> None:
    provider = credential.provider
    secret = decrypt_secret(credential)
    # All current callers (sentry fix, code review, pr summary) are
    # high-reasoning workflows; use the high tier.
    model = credential.model_high
    base_url = credential.base_url or ""
    model_low = credential.model_low

    # The CLI echoes this back in its structured 429 log event so the
    # watcher knows exactly which pool row to mark stale.
    inputs.public_env["JEANCLODE_LLM_CREDENTIAL_ID"] = str(credential.id)

    if provider == "claude_code":
        inputs.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN] = secret
        _set_model_env(inputs, model, model_low)
        inputs.upstreams.append(_anthropic_oauth_upstream())
        return

    if provider == "anthropic":
        inputs.secrets[CredentialKey.ANTHROPIC_API_KEY] = secret
        _set_model_env(inputs, model, model_low)
        inputs.upstreams.append(_anthropic_apikey_upstream())
        return

    if provider in ("openai", "openai_compatible"):
        inputs.secrets[CredentialKey.OPENAI_API_KEY] = secret
        # Claude Code CLI requires ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL) to
        # skip the /login gate when using a third-party provider. Without it the
        # CLI returns "Not logged in" on every turn before making any model call.
        inputs.secrets["ANTHROPIC_AUTH_TOKEN"] = secret
        if model:
            inputs.public_env["OPENAI_MODEL"] = model
            # JEANCLODE_MODEL controls ctx.model in the runner, which becomes
            # the --model flag passed to Claude Code. Without this, ctx.model
            # defaults to "sonnet", which routes completions through the Anthropic
            # API client instead of the OpenAI-compatible path.
            inputs.public_env["JEANCLODE_MODEL"] = model
        if model_low:
            inputs.public_env["JEANCLODE_SMALL_MODEL"] = model_low
        if base_url:
            inputs.public_env["OPENAI_BASE_URL"] = base_url
            inputs.public_env["ANTHROPIC_BASE_URL"] = base_url
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=CredentialKey.OPENAI_API_KEY,
                host=host_or("api.openai.com", base_url),
                header="Authorization",
                bearer=True,
            )
        )
        inputs.public_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"


_ANTHROPIC_HOST = "api.anthropic.com"


def _llm_host(credential: LLMCredential) -> str:
    if credential.provider in ("claude_code", "anthropic"):
        return _ANTHROPIC_HOST
    return host_or("api.openai.com", credential.base_url)


def _llm_option_name(credential: LLMCredential) -> str:
    if credential.name:
        return credential.name
    if credential.provider in ("openai", "openai_compatible") and credential.base_url:
        return f"{credential.provider}@{_llm_host(credential)}"
    return credential.provider


def _add_llm_options(
    inputs: DispatchInputs, primary: LLMCredential, pool: list[LLMCredential]
) -> None:
    """Expose every distinct credential to the run so triage can steer the fixer (#43).

    The proxy injects one credential per host, so credentials sharing a host
    (several Claude subscriptions, an Anthropic key next to one) collapse to
    the first usable one, the primary winning. Each extra credential gets its
    own secret name, and ``JEANCLODE_LLM_OPTIONS`` carries only public data:
    the env a fixer session sets to target it, secrets referenced by name.
    Nothing is emitted unless there is an actual choice to make.
    """
    now = datetime.now(UTC)
    seen = {_llm_host(primary)}
    options: list[dict[str, Any]] = [_llm_option(primary, env={}, secret_env={})]
    for credential in pool:
        if credential.id == primary.id or llm_credential_is_stale(credential, now):
            continue
        host = _llm_host(credential)
        if host in seen:
            continue
        seen.add(host)
        secret_key = f"JEANCLODE_LLM_OPTION_{len(options)}_SECRET"
        inputs.secrets[secret_key] = decrypt_secret(credential)
        env, secret_env, upstream = _llm_option_wiring(credential, secret_key, host)
        inputs.upstreams.append(upstream)
        options.append(_llm_option(credential, env=env, secret_env=secret_env))

    if len(options) > 1 or primary.model_heavy:
        inputs.public_env["JEANCLODE_LLM_OPTIONS"] = json.dumps(options)


def _llm_option(
    credential: LLMCredential, *, env: dict[str, str], secret_env: dict[str, str]
) -> dict[str, Any]:
    return {
        "id": str(credential.id),
        "name": _llm_option_name(credential),
        "provider": credential.provider,
        "model_high": credential.model_high,
        "model_heavy": credential.model_heavy,
        "model_low": credential.model_low,
        "env": env,
        "secret_env": secret_env,
    }


def _llm_option_wiring(
    credential: LLMCredential, secret_key: str, host: str
) -> tuple[dict[str, str], dict[str, str], UpstreamCredential]:
    """Session env, session-var -> secret-name map, and proxy rule for one extra credential.

    Empty values blank out whatever the primary credential exported, so a
    fixer session never authenticates two ways at once.
    """
    if credential.provider == "claude_code":
        env = {
            "ANTHROPIC_BASE_URL": f"https://{_ANTHROPIC_HOST}",
            "ANTHROPIC_AUTH_TOKEN": "",
            "ANTHROPIC_API_KEY": "",
        }
        upstream = UpstreamCredential(
            secret_key=secret_key, host=host, header="Authorization", bearer=True
        )
        return env, {"CLAUDE_CODE_OAUTH_TOKEN": secret_key}, upstream

    if credential.provider == "anthropic":
        env = {
            "ANTHROPIC_BASE_URL": f"https://{_ANTHROPIC_HOST}",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
        }
        upstream = UpstreamCredential(secret_key=secret_key, host=host, header="x-api-key")
        return env, {"ANTHROPIC_API_KEY": secret_key}, upstream

    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if credential.model_high:
        env["OPENAI_MODEL"] = credential.model_high
    if credential.base_url:
        env["OPENAI_BASE_URL"] = credential.base_url
        env["ANTHROPIC_BASE_URL"] = credential.base_url
    upstream = UpstreamCredential(
        secret_key=secret_key, host=host, header="Authorization", bearer=True
    )
    return env, {"ANTHROPIC_AUTH_TOKEN": secret_key, "OPENAI_API_KEY": secret_key}, upstream


def _anthropic_oauth_upstream() -> UpstreamCredential:
    return UpstreamCredential(
        secret_key=CredentialKey.CLAUDE_CODE_OAUTH_TOKEN,
        host="api.anthropic.com",
        header="Authorization",
        bearer=True,
    )


def _anthropic_apikey_upstream() -> UpstreamCredential:
    return UpstreamCredential(
        secret_key=CredentialKey.ANTHROPIC_API_KEY,
        host="api.anthropic.com",
        header="x-api-key",
    )


# ---------------------------------------------------------------------------
# Git platform
# ---------------------------------------------------------------------------


async def add_git_platform_to_inputs(
    inputs: DispatchInputs,
    *,
    git_org_id: UUID,
    repo_token_override_encrypted: str | None = None,
) -> None:
    """Resolve the git provider credential for a given git org.

    GitHub: short-lived installation token via the GitHub App. Injected
    on both ``api.github.com`` (REST/GraphQL via gh) and ``github.com``
    (the git HTTPS endpoint that gh shells into for the actual clone).
    GitLab: PAT decrypted from the org row, mirrored across its API and
    git host. Logs are loud on every guard so a missing token is
    diagnosable from a single failed dispatch.

    ``repo_token_override_encrypted`` lets a caller (Sentry's mapped repo
    layer) supply a per-repo encrypted token that takes precedence over
    the org-level one — same precedence rule the previous in-tree helper
    enforced.
    """
    from api.database.organization import db_get_org_by_id

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.warning("git platform creds skipped: database plugin not loaded")
        return

    with db_plugin.session() as db:
        git_org = db_get_org_by_id(db, git_org_id)
        if not git_org:
            logger.info("git platform creds skipped: git org %s missing", git_org_id)
            return
        provider = git_org.provider
        installation_id = git_org.installation_id
        encrypted_token = repo_token_override_encrypted or git_org.auth_token_encrypted
        base_url = git_org.base_url

    if provider == "github":
        if not installation_id:
            logger.warning(
                "git platform creds skipped: github org %s has no installation_id",
                git_org_id,
            )
            return
        if not app.github:
            logger.warning(
                "git platform creds skipped: github plugin not loaded (check GITHUB_ENABLED + GITHUB_APP_*)"
            )
            return
        try:
            token = await app.github.get_installation_access_token(installation_id)
        except Exception:
            logger.exception(
                "Failed to get GitHub installation token for installation_id=%s",
                installation_id,
            )
            return
        # GitHub Cloud splits API and git over two hosts (api.github.com
        # vs github.com); GHES collapses both onto the org's configured
        # base host. Treating them uniformly via ``host_or(base_url)`` was
        # a bug for Cloud — it pinned both to ``github.com`` and the
        # GraphQL call flew unauthenticated.
        base_host = host_or("github.com", base_url)
        if base_host == "github.com":
            api_host, git_host = "api.github.com", "github.com"
        else:
            api_host = git_host = base_host
        inputs.secrets[CredentialKey.GH_TOKEN] = token
        if base_host != "github.com":
            inputs.public_env["GH_HOST"] = host_port_or("github.com", base_url)

        # github.com git smart HTTP rejects Bearer for installation tokens.
        gh_basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        inputs.secrets["GH_GIT_AUTH"] = f"Basic {gh_basic}"

        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=CredentialKey.GH_TOKEN,
                host=api_host,
                header="Authorization",
                bearer=True,
            )
        )
        if git_host != api_host:
            inputs.upstreams.append(
                UpstreamCredential(
                    secret_key="GH_GIT_AUTH",
                    host=git_host,
                    header="Authorization",
                    bearer=False,
                )
            )

        logger.info(
            "git platform creds resolved: github installation_id=%s, hosts=%s",
            installation_id,
            sorted({api_host, git_host}),
        )
        return

    if provider == "gitlab" and encrypted_token:
        try:
            token = db_plugin.decrypt(encrypted_token)
        except Exception:
            logger.exception("Failed to decrypt GitLab token")
            return
        host = host_or("gitlab.com", base_url)
        inputs.secrets[CredentialKey.GITLAB_TOKEN] = token
        if base_url:
            inputs.public_env["GITLAB_URL"] = base_url
        if host != "gitlab.com":
            inputs.public_env["GITLAB_HOST"] = host_port_or("gitlab.com", base_url)
        # Two headers, both injected on every gitlab.com request:
        # - PRIVATE-TOKEN: needed for /api/v4/* (API rejects Basic for project tokens).
        # - Authorization: Basic — needed for git smart HTTP (which only speaks Authorization).
        # Each endpoint uses the one it understands; the other is ignored.
        gl_basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        inputs.secrets["GITLAB_GIT_AUTH"] = f"Basic {gl_basic}"
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=CredentialKey.GITLAB_TOKEN,
                host=host,
                header="PRIVATE-TOKEN",
                bearer=False,
            )
        )
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key="GITLAB_GIT_AUTH",
                host=host,
                header="Authorization",
                bearer=False,
            )
        )
        logger.info("git platform creds resolved: gitlab host=%s", host)
        return

    logger.warning(
        "git platform creds skipped: provider=%s installation_id=%s has_encrypted_token=%s",
        provider,
        installation_id,
        bool(encrypted_token),
    )


def _connected_org_component(all_orgs: list[Any], start_ids: set[UUID]) -> set[UUID]:
    """The full ``parent_org_id`` connected component(s) containing ``start_ids``.

    Treats the hierarchy as undirected: a GitLab group access token cascades
    to every subgroup beneath whichever ancestor holds it (siblings
    included), so once an org is in scope its entire top-level group tree
    needs to be — not just the path down to whichever repo triggered the
    dispatch. Distinct top-level groups (no ``parent_org_id`` edge between
    them) never merge into the same component even if they share a
    ``workspace_id`` — that's exactly the boundary this scoping enforces.

    Operates on an already-fetched org list rather than querying
    ancestors/descendants one at a time, so this costs no extra round trips
    beyond the one ``db_get_orgs_by_workspace`` call already made.
    """
    by_id = {org.id: org for org in all_orgs}
    neighbors: dict[UUID, set[UUID]] = {}
    for org in all_orgs:
        if org.parent_org_id and org.parent_org_id in by_id:
            neighbors.setdefault(org.id, set()).add(org.parent_org_id)
            neighbors.setdefault(org.parent_org_id, set()).add(org.id)

    component: set[UUID] = set()
    queue = [org_id for org_id in start_ids if org_id in by_id]
    while queue:
        node = queue.pop()
        if node in component:
            continue
        component.add(node)
        queue.extend(neighbors.get(node, ()))
    return component


def _resolve_scoped_gitlab_orgs(
    db: Any, trigger_org: Any, workspace_id: UUID | None
) -> tuple[list[Any], list[Any]]:
    """The GitLab orgs/repos a dispatch for ``trigger_org`` may need credentials for.

    Not "every org in the workspace" — that let a completely unrelated org
    sharing only a ``workspace_id`` (e.g. a different internal team's GitLab
    group, connected by the same tenant account) balloon a single dispatch's
    credential secret past Kubernetes' 1 MiB cap (see the incident that
    prompted this: an ingester MR pulled in 80+ repos from an unrelated
    team). Scope is instead:

    1. ``trigger_org``'s own connected component in the ``parent_org_id``
       hierarchy — its whole top-level group tree, ancestors and
       descendants, since a group token cascades to every subgroup beneath
       it (see :func:`_connected_org_component`). Returned as
       ``orgs_in_scope`` — these get full per-org treatment (their own
       namespace prefix, every repo under them).
    2. One hop across explicit repo links (``db_link_repos`` /
       ``RepositoryMapping``) from any repo owned by an org in (1) — ADR-003's
       "related-repo groups may span groups" feature. Returned separately as
       ``linked_repos``, credentialed narrowly to just that repo (its own
       path + numeric id, whatever token covers it) rather than pulled in as
       a whole org: an org can legitimately own hundreds of unrelated repos
       under one company-wide root group, and a tenant linking one of them
       authorizes exactly that repo, not blanket access to every other repo
       its owning org happens to also hold. (An earlier version of this
       function pulled in the linked org's whole connected component, which
       silently reintroduced the same blast radius as the original incident
       the moment a link happened to touch a root org with hundreds of
       repos.) Not chased transitively: a link is a tenant's explicit "these
       belong together" declaration for one pair, not a standing invitation
       to walk the whole graph of everything ever linked to anything.

    Falls back to ``([trigger_org], [])`` when it has no ``workspace_id`` at all.
    """
    from api.database.organization import db_get_orgs_by_workspace
    from api.database.repository import db_get_related_repos_bulk, db_get_repositories_by_org

    if not workspace_id:
        return [trigger_org], []

    all_workspace_orgs = db_get_orgs_by_workspace(
        db, workspace_id, provider="gitlab", require_token=False
    )
    relevant_ids = _connected_org_component(all_workspace_orgs, {trigger_org.id})

    tree_repo_ids = [
        repo.id for org_id in relevant_ids for repo in db_get_repositories_by_org(db, org_id)
    ]
    linked_repos: list[Any] = []
    if tree_repo_ids:
        related = db_get_related_repos_bulk(db, tree_repo_ids)
        seen_repo_ids: set[UUID] = set()
        for repos in related.values():
            for repo in repos:
                if repo.org_id in relevant_ids or repo.id in seen_repo_ids:
                    continue
                seen_repo_ids.add(repo.id)
                linked_repos.append(repo)

    orgs_in_scope = [org for org in all_workspace_orgs if org.id in relevant_ids]
    return orgs_in_scope, linked_repos


async def add_gitlab_workspace_credentials(
    inputs: DispatchInputs,
    *,
    git_org_id: UUID,
    execution_id: UUID | None = None,
) -> None:
    """Bundle GitLab tokens for the orgs relevant to this dispatch into the
    proxy config — the trigger org's own connected group tree, plus any org
    reached by an explicit repo link (see :func:`_resolve_scoped_gitlab_orgs`).

    Each org contributes two upstream entries (PRIVATE-TOKEN for the API,
    Authorization Basic for git smart HTTP), both scoped to that org's
    namespace path via ``path_prefix``. Organizations outside this dispatch's
    scope never share a proxy config with it.

    Falls back to the trigger org only when it has no ``workspace_id``.

    Token resolution per org:
    - Org-level token → one namespace-prefix entry (``/my-group/``) covers
      every repo under it, including tokenless descendant subgroups, since
      the proxy's longest-prefix match is a plain ``startswith``. No
      additional per-repo namespace entry is emitted — it would carry the
      identical token and add no routing coverage the org-level prefix
      doesn't already provide. Exception: a legacy org row whose
      ``external_org_id`` is a bare numeric group id (not a namespace path)
      gets no org-level prefix at all, so each of its repos' own
      ``Repository.name`` (``path_with_namespace``) prefix is the only
      source of namespace coverage and is kept.
    - No org token → fall back to repo-level tokens; each repo gets its own
      per-repo prefix (e.g. ``/my-group/my-repo/``). The proxy longest-prefix
      rule ensures the right token is selected per request.

    Each repo also contributes a second, numeric-id-keyed entry for the same
    token (``/api/v4/projects/<external_id>/``). `glab api -R <repo>
    "projects/:id/..."` — the standard way to call the GitLab REST API for a
    known repo — resolves ``:id`` by first doing ``GET /api/v4/projects/<slug>``,
    then substituting the numeric id that returns into the real request. That
    follow-up carries no namespace for the slug-keyed prefix to match, so
    without this second entry every such call fails closed even though it's
    for a repo we already hold a token for. ``Repository.external_id`` is
    already the numeric GitLab project id (set from the sync/connect flows),
    so this needs no new data — just a second ``path_prefix`` per repo.
    """
    from api.database.organization import (
        db_get_descendant_orgs,
        db_get_org_by_id,
        db_resolve_org_token,
    )
    from api.database.repository import db_get_repositories_by_org

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        logger.warning(
            "gitlab workspace creds skipped: database plugin not loaded (execution=%s)",
            execution_id,
        )
        return

    with db_plugin.session() as db:
        trigger_org = db_get_org_by_id(db, git_org_id)
        if not trigger_org:
            logger.info(
                "gitlab workspace creds skipped: org %s missing (execution=%s)",
                git_org_id,
                execution_id,
            )
            return
        if trigger_org.provider != "gitlab":
            logger.warning(
                "gitlab workspace creds skipped: org %s is provider=%s (execution=%s)",
                git_org_id,
                trigger_org.provider,
                execution_id,
            )
            return

        workspace_id = trigger_org.workspace_id
        base_url = trigger_org.base_url

        gitlab_orgs, linked_repos = _resolve_scoped_gitlab_orgs(db, trigger_org, workspace_id)

        # Each entry: (path_prefix, encrypted_token, host, log_label)
        cred_entries: list[tuple[str, str, str, str]] = []
        for org in gitlab_orgs:
            org_host = host_or("gitlab.com", org.base_url)
            if org.auth_token_encrypted:
                # ``external_org_id`` is GitLab's numeric group id, not a
                # namespace path — a prefix built from it matches no real
                # request. Keep it only for the rare legacy row that stored a
                # path, and derive the namespace prefixes from the repos we
                # track instead (``Repository.name`` is path_with_namespace).
                has_org_namespace_prefix = not org.external_org_id.strip("/").isdigit()
                if has_org_namespace_prefix:
                    cred_entries.append(
                        (
                            f"/{org.external_org_id.strip('/')}/",
                            org.auth_token_encrypted,
                            org_host,
                            f"org={org.id} namespace={org.external_org_id}",
                        )
                    )
                # A group token covers its subgroups, so the descendants that
                # were demoted to placeholders when this token was connected
                # get their repos routed through it too.
                covered_repos = list(db_get_repositories_by_org(db, org.id))
                if workspace_id:
                    for descendant in db_get_descendant_orgs(db, org.id, workspace_id):
                        if not descendant.auth_token_encrypted:
                            covered_repos.extend(db_get_repositories_by_org(db, descendant.id))
                for repo in covered_repos:
                    # A per-repo namespace entry would carry the exact same
                    # token as the org-level prefix above, which already
                    # matches every path under it (the proxy's longest-prefix
                    # match is a plain ``startswith``) — adding it here would
                    # only duplicate that token in the secret with no new
                    # routing coverage. Only emit it when there was no
                    # org-level prefix to begin with (numeric-external-org-id
                    # legacy rows), where it's the sole source of namespace
                    # coverage for that repo.
                    if not has_org_namespace_prefix:
                        cred_entries.append(
                            (
                                f"/{repo.name.strip('/')}/",
                                org.auth_token_encrypted,
                                org_host,
                                f"org={org.id} repo={repo.name}",
                            )
                        )
                    if repo.external_id:
                        cred_entries.append(
                            (
                                f"/api/v4/projects/{repo.external_id}/",
                                org.auth_token_encrypted,
                                org_host,
                                f"org={org.id} repo={repo.name} id={repo.external_id}",
                            )
                        )
            else:
                repos = db_get_repositories_by_org(db, org.id)
                for repo in repos:
                    if repo.auth_token_encrypted:
                        repo_host = host_or("gitlab.com", repo.provider_url or org.base_url)
                        path_prefix = f"/{repo.name.strip('/')}/"
                        cred_entries.append(
                            (
                                path_prefix,
                                repo.auth_token_encrypted,
                                repo_host,
                                f"org={org.id} repo={repo.name}",
                            )
                        )
                        if repo.external_id:
                            cred_entries.append(
                                (
                                    f"/api/v4/projects/{repo.external_id}/",
                                    repo.auth_token_encrypted,
                                    repo_host,
                                    f"org={org.id} repo={repo.name} id={repo.external_id}",
                                )
                            )

        # Explicit repo links (see _resolve_scoped_gitlab_orgs) are
        # credentialed narrowly — just this one repo's own path + numeric id,
        # with whatever token actually covers it (its own, or its org's,
        # walking that org's own ancestor chain). Never the linked org's
        # namespace prefix: that org may hold hundreds of other repos this
        # link says nothing about.
        for repo in linked_repos:
            linked_org = db_get_org_by_id(db, repo.org_id)
            if not linked_org:
                continue
            token = repo.auth_token_encrypted or db_resolve_org_token(db, linked_org)
            if not token:
                continue
            repo_host = host_or("gitlab.com", repo.provider_url or linked_org.base_url)
            cred_entries.append(
                (
                    f"/{repo.name.strip('/')}/",
                    token,
                    repo_host,
                    f"linked repo={repo.name} org={linked_org.id}",
                )
            )
            if repo.external_id:
                cred_entries.append(
                    (
                        f"/api/v4/projects/{repo.external_id}/",
                        token,
                        repo_host,
                        f"linked repo={repo.name} id={repo.external_id}",
                    )
                )

    if base_url:
        inputs.public_env["GITLAB_URL"] = base_url
        gl_host = host_or("gitlab.com", base_url)
        if gl_host != "gitlab.com":
            inputs.public_env["GITLAB_HOST"] = host_port_or("gitlab.com", base_url)

    if not cred_entries:
        logger.warning(
            "gitlab workspace creds resolved to zero tokens for org %s "
            "(workspace=%s execution=%s) — the proxy will deny all GitLab hosts "
            "for this run",
            git_org_id,
            workspace_id,
            execution_id,
        )

    # Many path-prefix entries (one per repo, for numeric-id routing) can
    # carry the identical org token — a distinct GITLAB_TOKEN_<n> secret key
    # per *entry* would duplicate that same literal token hundreds of times
    # over into the k8s Secret (which is what's capped at 1 MiB; the proxy's
    # own upstream config only ever holds ``${VAR}`` placeholders, never the
    # value). Keyed by the encrypted ciphertext itself: `cred_entries` never
    # re-encrypts a token, it just re-reads the same `Organization`/
    # `Repository` field for each repo that token covers, so identical
    # ciphertext reliably means "the same token", not a coincidence.
    keys_by_token: dict[str, tuple[str, str]] = {}
    for path_prefix, encrypted_token, host, label in cred_entries:
        keys = keys_by_token.get(encrypted_token)
        if keys is None:
            try:
                token = db_plugin.decrypt(encrypted_token)
            except Exception:
                logger.exception(
                    "gitlab workspace creds: failed to decrypt token for %s (execution=%s)",
                    label,
                    execution_id,
                )
                continue

            idx = len(keys_by_token)
            token_key = f"GITLAB_TOKEN_{idx}"
            git_auth_key = f"GITLAB_GIT_AUTH_{idx}"
            gl_basic = base64.b64encode(f"oauth2:{token}".encode()).decode()

            inputs.secrets[token_key] = token
            inputs.secrets[git_auth_key] = f"Basic {gl_basic}"
            keys = (token_key, git_auth_key)
            keys_by_token[encrypted_token] = keys

        token_key, git_auth_key = keys
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=token_key,
                host=host,
                header="PRIVATE-TOKEN",
                bearer=False,
                path_prefix=path_prefix,
            )
        )
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=git_auth_key,
                host=host,
                header="Authorization",
                bearer=False,
                path_prefix=path_prefix,
            )
        )
        logger.info(
            "gitlab workspace creds resolved: %s host=%s prefix=%s execution=%s",
            label,
            host,
            path_prefix,
            execution_id,
        )

    # glab exits with "Unauthenticated." before making any HTTP request when
    # GITLAB_TOKEN is absent — the proxy never gets to inject. A dummy value
    # is enough: the proxy strips whatever glab sends and injects the real
    # token from the sidecar (GITLAB_TOKEN_0 etc.) anyway.
    if cred_entries:
        inputs.public_env.setdefault("GITLAB_TOKEN", "proxy-injected")


async def add_plugin_marketplace_credentials(
    inputs: DispatchInputs,
    *,
    git_org_id: UUID,
    git_urls: list[str],
) -> None:
    """Credential the third-party plugin repos the CLI clones in-container.

    A marketplace has no token of its own — the dashboard never asks for one
    — so a plugin repo is reached with the git org's token, the same one
    ``resolve_third_party_plugins_env_for_org`` already uses to read the
    manifest. Allowlisting the host through ``extra_hosts`` is not enough on
    its own: an ``internal`` GitLab repo still needs auth, and a marketplace
    typically lives outside the org's own namespace, so none of the
    per-namespace rules from ``add_gitlab_workspace_credentials`` match it.

    Scoped to each plugin repo's own path rather than the whole host, so a
    marketplace can't borrow the token for unrelated projects.
    """
    from api.database.organization import db_get_org_by_id
    from api.plugins.container.utils import _get_dispatch_git_token

    if not git_urls:
        return

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, git_org_id)
        provider = org.provider if org else None

    if provider not in ("github", "gitlab"):
        logger.info("plugin marketplace creds skipped: org %s provider=%s", git_org_id, provider)
        return

    token = await _get_dispatch_git_token(git_org_id)
    if not token:
        logger.warning(
            "plugin marketplace creds skipped: no git token for org %s — "
            "cloning a private or internal plugin repo will fail",
            git_org_id,
        )
        return

    seen: set[tuple[str, str]] = set()
    for git_url in git_urls:
        parsed = urlparse(git_url)
        host = (parsed.hostname or "").lower()
        repo_path = parsed.path.removesuffix(".git").strip("/")
        if not host or not repo_path:
            continue
        path_prefix = f"/{repo_path}/"
        if (host, path_prefix) in seen:
            continue
        idx = len(seen)
        seen.add((host, path_prefix))

        basic_user = "oauth2" if provider == "gitlab" else "x-access-token"
        auth_key = f"PLUGIN_GIT_AUTH_{idx}"
        inputs.secrets[auth_key] = (
            "Basic " + base64.b64encode(f"{basic_user}:{token}".encode()).decode()
        )
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=auth_key,
                host=host,
                header="Authorization",
                bearer=False,
                path_prefix=path_prefix,
            )
        )
        if provider == "gitlab":
            # The CLI only clones a plugin repo, but a marketplace manifest
            # refetch from inside the container would hit the REST API, which
            # rejects Basic for project tokens.
            token_key = f"PLUGIN_TOKEN_{idx}"
            inputs.secrets[token_key] = token
            inputs.upstreams.append(
                UpstreamCredential(
                    secret_key=token_key,
                    host=host,
                    header="PRIVATE-TOKEN",
                    bearer=False,
                    path_prefix=path_prefix,
                )
            )
        logger.info(
            "plugin marketplace creds resolved: org=%s host=%s prefix=%s",
            git_org_id,
            host,
            path_prefix,
        )


def add_agent_tooling_hosts(inputs: DispatchInputs) -> None:
    """Allowlist the public hosts the Claude Code subprocess always touches."""
    inputs.extra_hosts.extend(AGENT_TOOLING_HOSTS)


# ---------------------------------------------------------------------------
# Memory (container-initiated /internal/memory API, #181)
# ---------------------------------------------------------------------------


async def add_memory_to_inputs(
    inputs: DispatchInputs,
    *,
    workspace_id: UUID,
    execution_id: UUID,
) -> None:
    """Mint a per-execution memory API token and wire it into the sidecar.

    Token goes in inputs.secrets (sidecar-only); only the URL goes in
    public_env. No-ops with a warning if the database or backend_url isn't
    configured — the signing secret itself is always available, generated
    and persisted on first use (see get_or_create_memory_signing_secret).
    """
    from api.services.instance_settings import get_or_create_memory_signing_secret

    app = get_current_app()
    backend_url = app.options.backend_url
    if not backend_url:
        logger.warning(
            "memory creds skipped: no backend_url configured (Options.backend_url / BACKEND_URL)"
        )
        return
    if not app.database:
        logger.warning("memory creds skipped: database plugin not loaded")
        return

    with app.database.session() as db:
        secret = get_or_create_memory_signing_secret(db)

    token = mint_memory_token(workspace_id=workspace_id, execution_id=execution_id, secret=secret)
    host = host_or("localhost", backend_url)

    inputs.secrets[CredentialKey.MEMORY_API_TOKEN] = token
    inputs.upstreams.append(
        UpstreamCredential(
            secret_key=CredentialKey.MEMORY_API_TOKEN,
            host=host,
            header="Authorization",
            bearer=True,
        )
    )
    inputs.public_env["JEANCLODE_MEMORY_API_URL"] = backend_url.rstrip("/")

    logger.info(
        "memory creds resolved: workspace=%s execution=%s host=%s",
        workspace_id,
        execution_id,
        host,
    )


# ---------------------------------------------------------------------------
# Connectors: MCP servers + skill credentials (#191)
# ---------------------------------------------------------------------------


def _wire_static_credential(
    inputs: DispatchInputs,
    *,
    secret_key: str,
    host: str,
    auth_type: str,
    secret: dict,
    settings: dict,
    path_prefix: str | None = None,
) -> None:
    """api_key/jwt/basic_auth — one static header value, reusing the
    existing ``${VAR}``-templating ``UpstreamCredential`` path unchanged."""
    if auth_type == AuthType.BASIC_AUTH.value:
        basic = base64.b64encode(f"{secret['username']}:{secret['password']}".encode()).decode()
        inputs.secrets[secret_key] = f"Basic {basic}"
        inputs.upstreams.append(
            UpstreamCredential(
                secret_key=secret_key,
                host=host,
                header="Authorization",
                bearer=False,
                path_prefix=path_prefix,
            )
        )
        return

    header = settings.get("header", "Authorization")
    value_prefix = settings.get("value_prefix", "Bearer ")
    inputs.secrets[secret_key] = secret["key"]
    inputs.upstreams.append(
        UpstreamCredential(
            secret_key=secret_key,
            host=host,
            header=header,
            bearer=(value_prefix == "Bearer "),
            path_prefix=path_prefix,
        )
    )


def _wire_oauth_credential(
    inputs: DispatchInputs,
    *,
    secret_key: str,
    host: str,
    secret: dict,
    settings: dict,
    path_prefix: str | None = None,
) -> None:
    """oauth2 — the proxy mints the token itself; the backend only ever
    handles the credential fields below, never the actual access token.

    Which of ``username``/``password``/``refresh_token`` are present in
    ``secret`` depends on ``grant_type`` (see ``OAuth2Secret``'s
    cross-field validation in the connectors router) — only wire the keys
    that are actually there, rather than writing empty-string secrets for
    the fields a given grant type doesn't use.
    """
    client_id_key = f"{secret_key}_CID"
    client_secret_key = f"{secret_key}_CSEC"
    inputs.secrets[client_id_key] = secret["client_id"]
    inputs.secrets[client_secret_key] = secret["client_secret"]

    username_key = password_key = refresh_token_key = None
    if secret.get("username"):
        username_key = f"{secret_key}_USER"
        inputs.secrets[username_key] = secret["username"]
    if secret.get("password"):
        password_key = f"{secret_key}_PASS"
        inputs.secrets[password_key] = secret["password"]
    if secret.get("refresh_token"):
        refresh_token_key = f"{secret_key}_RTOK"
        inputs.secrets[refresh_token_key] = secret["refresh_token"]

    inputs.oauth_upstreams.append(
        OAuthUpstream(
            client_id_key=client_id_key,
            client_secret_key=client_secret_key,
            username_key=username_key,
            password_key=password_key,
            refresh_token_key=refresh_token_key,
            host=host,
            token_url=settings["token_url"],
            grant_type=settings.get("grant_type", "client_credentials"),
            scope=settings.get("scope"),
            path_prefix=path_prefix,
        )
    )


def _mcp_auth_scheme(cred: object, settings: dict) -> str:
    """How the CLI should format the placeholder value in the MCP entry's
    header: ``bearer`` (``Bearer <value>``), ``basic`` (``Basic <value>``,
    the CLI's placeholder is discarded either way since the proxy mints/
    injects the real ``Basic ...`` value), or ``raw`` (bare value, no
    prefix). The proxy replaces the actual bytes on the wire regardless —
    this only has to look plausible enough for a client library that
    validates header shape before sending.
    """
    auth_type = cred.auth_type  # type: ignore[attr-defined]
    if auth_type == AuthType.BASIC_AUTH.value:
        return "basic"
    if auth_type == AuthType.OAUTH2.value:
        return "bearer"
    return "bearer" if settings.get("value_prefix", "Bearer ") == "Bearer " else "raw"


async def add_connectors_to_inputs(inputs: DispatchInputs, *, git_org_id: UUID) -> None:
    """Resolve MCP servers + skill credentials for an org onto the dispatch.

    See https://github.com/jeanclode-hq/jeanclode/issues/14. Static auth
    types (api_key/basic_auth/jwt) reuse the existing ``${VAR}``-templating
    ``UpstreamCredential`` path — for a skill credential, the secret's own
    env var name doubles as the sidecar ``secret_key``, so the agent gets
    its placeholder for free via the existing per-secret-key placeholder
    mechanism (``kubernetes.py:_apply_sandbox_to_agent``) with no separate
    ``public_env`` write needed. ``oauth2`` rows become ``OAuthUpstream``
    entries — the proxy mints the token itself per execution.

    MCP servers are additionally surfaced to the CLI as a
    ``JEANCLODE_MCP_SERVERS`` JSON payload (name/url/header/auth_scheme per
    server) so it can register them — including servers with no
    credential, registered unauthenticated. The literal placeholder value
    the CLI sends is irrelevant (the proxy replaces it unconditionally on
    the wire for a matched upstream); the CLI needs no per-server env var,
    only ``auth_scheme`` to format a plausible-looking header.

    A subject can hold several credentials, one per host. ``none`` rows only
    allowlist their host. On a skill, ``basic_auth`` takes an optional env
    var name and ``oauth2`` is skipped with a warning (not wired for skills
    yet); on an MCP server, a credential for a host other than the server's
    own is wired to that host, for the server's tools to call.
    """
    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return

    from api.database.organization import db_get_connection_org_id

    with db_plugin.session() as db:
        # Connectors live on the org the tenant connected. A run inside a
        # GitLab subgroup is keyed to that subgroup's org, which holds none
        # of them, so resolve up to the connection first.
        config_org_id = db_get_connection_org_id(db, git_org_id)
        mcp_servers = db_get_mcp_servers_by_org(db, config_org_id)
        credentials = db_get_credentials_by_org(db, config_org_id)

    if not mcp_servers and not credentials:
        return

    creds_by_subject: dict[tuple[str, UUID], list[Credential]] = defaultdict(list)
    for c in credentials:
        creds_by_subject[(c.subject_type, c.subject_id)].append(c)

    # Only pay for the installations lookup when a credential actually
    # targets a skill — the common case (org has MCP servers or only
    # MCP-server credentials) never needs it.
    installations = []
    if any(c.subject_type == SubjectType.PLUGIN_INSTALLATION.value for c in credentials):
        with db_plugin.session() as db:
            installations = db_get_installations_by_org(db, config_org_id)

    mcp_payload: list[dict[str, object]] = []
    for idx, server in enumerate(mcp_servers):
        entry: dict[str, object] = {"name": server.name, "url": server.host}
        server_host = host_or(server.host, server.host)
        for n, cred in enumerate(
            creds_by_subject.get((SubjectType.MCP_SERVER.value, server.id), [])
        ):
            settings = cred.settings or {}
            host = host_or(server_host, settings.get("host"))
            if cred.auth_type == AuthType.NONE.value:
                inputs.extra_hosts.append(host)
                continue
            try:
                secret = json.loads(db_plugin.decrypt(cred.secret_encrypted))
            except Exception:
                logger.exception(
                    "Failed to decrypt credential %s for mcp_server %s", cred.id, server.id
                )
                continue
            # A credential on the server's own host is the one the MCP client
            # sends; any other is for a host the server's tools call out to.
            primary = host == server_host
            if primary:
                if cred.auth_type in (AuthType.API_KEY.value, AuthType.JWT.value):
                    entry["header"] = settings.get("header", "Authorization")
                else:
                    entry["header"] = "Authorization"
                entry["auth_scheme"] = _mcp_auth_scheme(cred, settings)
            # server.host is a full URL — two servers can share a host and
            # differ only by path (e.g. one gateway fronting multiple tools).
            # host_or() discards the path when resolving the allowlist host,
            # so path_prefix is how the proxy keeps their credentials apart.
            path_prefix = _url_path_prefix(server.host) if primary else None
            secret_key = f"MCP_{idx}" if n == 0 else f"MCP_{idx}_{n}"
            if cred.auth_type == AuthType.OAUTH2.value:
                _wire_oauth_credential(
                    inputs,
                    secret_key=secret_key,
                    host=host,
                    secret=secret,
                    settings=settings,
                    path_prefix=path_prefix,
                )
            else:
                _wire_static_credential(
                    inputs,
                    secret_key=secret_key,
                    host=host,
                    auth_type=cred.auth_type,
                    secret=secret,
                    settings=settings,
                    path_prefix=path_prefix,
                )
        mcp_payload.append(entry)

    if mcp_payload:
        inputs.public_env["JEANCLODE_MCP_SERVERS"] = json.dumps(mcp_payload)

    for install in installations:
        for cred in creds_by_subject.get((SubjectType.PLUGIN_INSTALLATION.value, install.id), []):
            _wire_skill_credential(inputs, cred, install_id=install.id, db_plugin=db_plugin)


def _wire_skill_credential(
    inputs: DispatchInputs, cred: Credential, *, install_id: UUID, db_plugin: Any
) -> None:
    settings = cred.settings or {}
    settings_host = settings.get("host")
    if not settings_host:
        logger.warning(
            "Skipping credential %s for skill %s — settings.host missing", cred.id, install_id
        )
        return
    host = host_or(settings_host, settings_host)
    if cred.auth_type == AuthType.NONE.value:
        inputs.extra_hosts.append(host)
        return
    if cred.auth_type == AuthType.OAUTH2.value:
        logger.warning(
            "Skipping oauth2 credential %s for skill %s — not wired for skills yet",
            cred.id,
            install_id,
        )
        return
    try:
        secret = json.loads(db_plugin.decrypt(cred.secret_encrypted))
    except Exception:
        logger.exception(
            "Failed to decrypt credential %s for plugin_installation %s", cred.id, install_id
        )
        return
    # The env var name doubles as the sidecar secret_key, so the agent gets a
    # placeholder under that name for free (kubernetes.py). Basic auth's name
    # is optional — the proxy injects the header whether or not a skill checks
    # for a variable.
    env_var_name = secret.get("name")
    if not env_var_name and cred.auth_type == AuthType.BASIC_AUTH.value:
        env_var_name = f"SKILL_AUTH_{cred.id.hex[:8].upper()}"
    if not env_var_name:
        logger.warning(
            "Skipping credential %s for skill %s — secret.name missing", cred.id, install_id
        )
        return
    _wire_static_credential(
        inputs,
        secret_key=env_var_name,
        host=host,
        auth_type=cred.auth_type,
        secret=secret,
        settings=settings,
    )


async def resolve_memory_workspace_id(org_id: UUID | None) -> UUID | None:
    """Return the workspace_id to wire memory credentials for, or None if
    the org has no workspace. Memory is always on — there's no opt-in."""
    if org_id is None:
        return None

    from api.database.organization import db_get_org_by_id

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return None

    with db_plugin.session() as db:
        org = db_get_org_by_id(db, org_id)
        return org.workspace_id if org else None


NOTIFY_USERS_ENV_VAR = "JEANCLODE_NOTIFY_USERS"


def add_notify_to_inputs(inputs: DispatchInputs, *, git_org_id: UUID) -> None:
    """Resolve the org's notify list to provider handles for the container.

    Settings are read through ``db_resolve_org_settings`` so a list set on
    a GitLab group reaches its subgroups' dispatches, and the ids are
    resolved against that same org chain's memberships — a handle that no
    longer belongs to the org is dropped rather than mentioned.

    No-ops silently when the list is empty, which is also the CLI's
    on/off signal: no env var, no ping.

    Every failure path is a no-op rather than a raise, ``get_current_app``
    included: a notification is the least important thing a dispatch
    carries, and it must never cost the tenant the MR itself.
    """

    def _resolve(db) -> list[str]:
        from api.database.organization import (
            db_get_org_by_id,
            db_resolve_notify_handles,
            db_resolve_org_settings,
        )
        from api.models.settings import GitOrgSettings

        org = db_get_org_by_id(db, git_org_id)
        if not org:
            return []
        settings = GitOrgSettings.model_validate(db_resolve_org_settings(db, org))
        return db_resolve_notify_handles(db, org, settings.notify.on_ready)

    try:
        app = get_current_app()
        if not app.database:
            logger.warning("notify list skipped: database plugin not loaded")
            return
        with app.database.session() as db:
            handles = _resolve(db)
    except Exception:
        logger.exception("Failed to resolve notify list for org %s", git_org_id)
        return

    if not handles:
        return

    inputs.public_env[NOTIFY_USERS_ENV_VAR] = json.dumps(handles)
    logger.info("notify list resolved for org %s: %d handle(s)", git_org_id, len(handles))
