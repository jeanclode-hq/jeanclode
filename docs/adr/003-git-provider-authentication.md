# ADR-003: Git Provider Authentication

## Status

Accepted

## Context

Jeanclode runs its agents in isolated, ephemeral containers per tenant. These containers need to read code (during triage) and write code + open PRs (during fix) across potentially multiple repositories and namespaces.

We support two git providers: GitHub and GitLab. They have fundamentally different integration models, and the authentication approach must work for both while maintaining multi-tenant security.

### Requirements

- Containers must authenticate to git providers without the backend ever handling code or tokens at runtime.
- Agents must be able to explore code across multiple repos/namespaces during triage.
- Agents must be able to push code and open PRs during the fix phase.
- Tokens must never leak across tenants.
- Authentication must be transparent to the agent, it should just use `git`, `gh`, and `glab` normally.

## Decision

### GitHub: App Installation

GitHub Apps provide a native integration model:

- Tenant installs the Jeanclode GitHub App on their org/repos.
- The App provides scoped permissions (read code, create PRs) across all installed repos — see ADR-008 for the additional Checks/Actions/Administration read scopes `ci-watch` needs.
- The backend generates a short-lived **installation access token** per container dispatch.
- The container receives one token that covers all installed repos.
- The token goes to the **security-proxy sidecar**, not to the container: `gh` and `git` run token-less and the proxy injects the `Authorization` header on the wire.

**Tenant lifecycle:** GitHub sends `installation` webhooks for install/uninstall/suspend events. The backend updates tenant state accordingly.

### GitLab: Group Access Tokens + Proxy Injection

GitLab has no equivalent to GitHub Apps. The approach:

1. **Tenant provides one Group Access Token per GitLab group** they want Jeanclode to access.
2. The backend stores these tokens (encrypted at rest).
3. At dispatch time, `add_gitlab_workspace_credentials` bundles the GitLab
   tokens *relevant to this dispatch* into the **security-proxy sidecar's**
   config — never into the agent container's environment. "Relevant" is not
   "every org sharing the trigger org's `workspace_id`": a workspace is a
   tenant-account-level boundary and can span multiple, unrelated internal
   teams' GitLab groups (see "Scope: connected component + explicit links"
   below) — bundling literally everything in it let one team's dispatch pull
   in another, completely unrelated team's tokens for zero reason, which is
   exactly what blew a real dispatch's secret past Kubernetes' 1 MiB cap.
4. Each org contributes two upstream entries, both scoped to that org's
   namespace by `path_prefix`: `PRIVATE-TOKEN` for the REST API, and
   `Authorization: Basic` for git smart HTTP.
5. `git` and `glab` are pointed at the proxy and run token-less. The proxy
   matches each request by host and longest path prefix and injects the right
   header on the wire.

#### Scope: connected component + explicit links

`add_gitlab_workspace_credentials` resolves scope in two steps, both
implemented in `_resolve_scoped_gitlab_orgs`/`_connected_org_component`
(`api/plugins/container/dispatch_inputs.py`):

1. **The trigger org's own connected component** in the `parent_org_id`
   hierarchy — its whole top-level group tree, ancestors and descendants,
   treated as an undirected graph. A GitLab group access token cascades to
   every subgroup beneath whichever ancestor holds it (siblings included), so
   once an org is in scope its entire top-level group tree needs to be, not
   just the path down to whichever repo triggered the dispatch. Two distinct
   top-level groups with no `parent_org_id` edge between them never merge
   into one component even if they share a `workspace_id` — that boundary is
   exactly what this scoping enforces.
2. **One hop across explicit repo links** (`db_link_repos` /
   `RepositoryMapping`) from any repo owned by an org in (1) — plus that
   linked org's own connected component, so its token inheritance resolves
   correctly too. Not chased transitively: a link is a tenant's explicit
   "these belong together" declaration for one pair, not a standing
   invitation to walk the whole graph of everything ever linked to anything.

This replaced an earlier design that bundled every GitLab org sharing the
trigger org's `workspace_id`, with no check that any of them were actually
related — broader reach for ad hoc cross-namespace triage, at the cost of an
unbounded credential set that scaled with the size of the *whole workspace*
rather than the repo actually being worked on.

The container never holds a real credential. Preflight hands `gh`/`glab` a
placeholder string only because both refuse to send a request with an empty
token — the proxy strips and rewrites the header regardless.

#### Per-repo numeric-id entries

Each repo also contributes a second entry keyed by its numeric project id
(`/api/v4/projects/<external_id>/`). `glab api -R <repo> "projects/:id/..."`
resolves `:id` by first fetching the project by slug, then re-issuing the real
request against the numeric id — and that follow-up carries no namespace for
the slug-keyed prefix to match. Without the second entry, every such call
fails closed against a repo whose token we already hold.

#### Cross-namespace access

During triage, an agent may need to read code across multiple namespaces within the *same* connected GitLab group tree (e.g., a bug in service A caused by a change in service B's API in a different subgroup of the same top-level group). Namespace-scoped prefixes handle this transparently: `git clone` for any repo in the trigger org's own connected component (see "Scope" above) just works.

If a bug traces to a namespace outside that scope — a different top-level group with no explicit repo link to it — no upstream matches, the request fails closed, and triage marks it as "no access" and skips it. This is a deliberate boundary, not a gap: a tenant connecting two unrelated internal teams' GitLab groups under one account must not give either team's dispatches implicit reach into the other's repos.

**Related-repo groups may span groups.** A repo can be grouped with another GitLab repo in a *different* group as long as both groups belong to the same workspace (`db_link_repos`). Dispatch follows that link one hop and pulls in the linked org's own connected component, each scoped to its own namespace prefix, so cloning the grouped repo authenticates against that group's own token with no extra wiring. Organizations neither in the trigger org's own tree nor reached by an explicit link never share a proxy config with it, even within the same workspace. GitHub stays single-org — one installation token can't span orgs — so cross-org grouping is GitLab-only.

**Tenant lifecycle:** No install/uninstall webhooks from GitLab. Token revocation is detected when the container gets 401 errors. The backend marks the affected group token as invalid and notifies the tenant.

## Security

### Token storage

- Tokens are encrypted at rest in the backend database.
- Tokens are only decrypted at container dispatch time and handed to the security-proxy sidecar — never to the agent container's environment.

### Container isolation

- Each container is ephemeral and scoped to a single tenant.
- No cross-tenant token mixing is possible, a container only receives tokens for its tenant.
- Containers are destroyed after use. No token persistence beyond the container lifecycle.

### Why the sidecar rather than an in-container credential

The agent process is the thing running model-generated code, so it is the
thing that must not hold a credential:

- Tokens live in the sidecar's config, in a separate container in the pod.
  Nothing in the agent's environment, filesystem or process table carries one.
- Tokens are never passed as CLI arguments (avoids `/proc` and shell history
  exposure).
- The proxy is an allowlist: a host with no configured upstream is refused, so
  a prompt-injected agent can't reach an arbitrary endpoint even without a
  credential to send it.
- Responses are scrubbed of any echoed `Authorization` header.

### Blast radius

- **GitHub:** Installation tokens are short-lived and scoped to installed repos. Compromise of a token gives access only to what the tenant already granted, and it expires quickly.
- **GitLab:** Group access tokens are scoped to a single group. Compromise gives access to that group only. Tenants control the scope by choosing which groups to provide tokens for.

### Agent token visibility

The agent cannot read a token, because there isn't one in its container to
read. The strongest thing it can do is make a request the proxy then
authenticates on its behalf — which is bounded by the allowlist and by the
namespace prefixes each token is scoped to.

## Consequences

- **GitHub integration is seamless**, App installation covers all repos, one token per container, standard `gh` CLI usage.
- **GitLab integration requires manual setup**, Tenants must provide one group access token per group. More friction but explicit and secure.
- **Cross-namespace exploration works for both**, GitHub via a single installation token, GitLab via the proxy routing each request to the right group token by namespace prefix.
- **No tokens in the agent container at all**, Tokens are decrypted only at dispatch time and live only in the sidecar, for the lifetime of the pod.
- **Token revocation detection differs**, GitHub has webhooks, GitLab relies on 401 detection at container runtime.
