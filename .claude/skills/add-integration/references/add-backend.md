# Adding Backend Support for an Integration

Every source or workflow that runs containers needs real backend support:
a plugin that's also an API client, a place to store the connection, a way
for a tenant to create that connection, and (usually) a way to receive or
poll for new work. Use the **Sentry plugin as the reference
implementation end to end** — it is the closest match for a new
issue-tracker source (Jira, Linear, PagerDuty):

```
backend/api/plugins/sentry/          plugin.py, config.py, dispatch.py, watcher.py, launch.py, models.py
backend/api/routers/sources/sentry/  route.py (connect a tenant), consumer.py, schemas.py
backend/api/routers/webhooks/sentry/ route.py, dependency.py (signature verify), installation.py
backend/api/models/organizations.py  Organization (shared across every provider)
backend/api/models/settings.py       GitOrgSettings / SentryOrgSettings (JSONB-backed)
backend/api/models/repositories.py   Repository, RepositoryMapping
backend/api/models/issues.py         Issue ("from any source")
```

**There is no separate `<Name>Watcher`-only integration point to bolt on.**
A source's backend plugin is a full `BasePlugin` subclass that is
simultaneously the API client, the dispatcher (if it polls), and the
container watcher. Don't design a "thin watcher wrapper" in isolation —
follow `SentryPlugin`'s shape from the start.

## Step 1: The Plugin Class

`backend/api/plugins/plugin.py` defines the contract every plugin
implements:

```python
class BasePlugin[ConfigT]:
    plugin_name: ClassVar[str]
    config_class: ClassVar[type[ConfigT]]
    priority: ClassVar[int] = 100   # lower starts first; Database=10, Sentry/OAuth=30, WebServer=50

    async def startup(self) -> None: ...       # abstract
    async def shutdown(self) -> None: ...      # abstract
    async def watch(self) -> None: ...         # optional — runs after ALL plugins have started
    def health_check(self) -> dict: ...
```

`SentryPlugin` (`backend/api/plugins/sentry/plugin.py`) subclasses
`BaseHttpPlugin[SentryPluginConfig]` and:
- `startup()` calls `super().startup()` (spins up the shared httpx
  client), then starts a `SentryDispatcher` if `config.dispatch.enabled`.
- `watch()` builds a `DockerBackend`/`KubernetesBackend` and starts the
  container watcher. **`watch()`, not `startup()`** — it runs after
  FastStream and the Container plugin are guaranteed up, which `startup()`
  order doesn't guarantee.
- `shutdown()` tears down watcher → dispatcher → `super().shutdown()`.
- The plugin itself owns the typed API methods (`list_organizations`,
  `get_organization`, `list_projects`, `list_issues`, `register_webhook`,
  ...) with its own retry/backoff/rate-limit handling. **The plugin is the
  API client** — there's no separate client module to write.

Register it: add the class to `BUILTIN_PLUGINS` in `backend/api/app.py`,
and (only if other code needs `app.jira`) add a typed property next to
`app.sentry`/`app.github` in the `Application` class.

Config: add `JiraPluginConfig` (mirror `backend/api/plugins/sentry/config.py`
— `enabled`, `base_url`, credentials, a `dispatch: JiraDispatchConfig`
sub-model if it polls, a `watcher: JiraWatcherConfig` sub-model). Either
add an explicit `jira: dict[str, Any]` field to `PluginsConfig`
(`backend/config.py`, matching the `sentry`/`github` fields) or rely on
its `extra="allow"` fallback — explicit is the existing convention. Add
the YAML block to `backend/configs/docker.yaml` and `kubernetes.yaml`
under `plugins:`, following the `sentry:` block there line for line
(`enabled`, `base_url`, credentials via `!ENV`, `dispatch: {enabled,
interval_seconds, batch_size, max_retries, max_concurrent_dispatches}`).

## Step 2: Storing the Connection — reuse `Organization`, don't invent a table

**There is one `Organization` table for every provider**
(`backend/api/models/organizations.py`), discriminated by a plain
`provider` string column (backed by `Provider(StrEnum)` — add
`JIRA = "jira"` there; the DB column itself is unconstrained, so this is a
code-only change, no migration). GitHub, GitLab, and Sentry orgs are all
rows in this one table. Relevant columns: `external_org_id`,
`installation_id` (nullable, unique — for App-install-style sources),
`base_url` (self-hosted), `auth_token_encrypted` /
`client_secret_encrypted`, `settings` (JSONB, provider-specific),
`onboarding_step`. Uniqueness is `(provider, external_org_id, base_url)`.

Encrypt tokens with `app.database.encrypt(token)` /
`.decrypt(...)` (`backend/api/plugins/database/plugin.py`, AES-256-GCM
keyed by `ENCRYPTION_KEY`) before writing to a `*_encrypted` column — there
is no separate encryption plugin.

**Settings**: add a `JiraOrgSettings(BaseModel)` to
`backend/api/models/settings.py`, mirroring `SentryOrgSettings` (triage
trigger mode, batch window/size if you poll-and-batch, `gate_on_open_fix_prs`
if relevant). Register it in `_SETTINGS_BY_PROVIDER` and in the
`OrgSettings` discriminated union (`_settings_discriminator`) — skipping
this makes `get_settings_model("jira")` raise `KeyError` and the generic
`PATCH /organizations/{id}/settings` endpoint 500 for Jira orgs.

**Modeling a Jira "project"**: don't invent a new table. `Repository`
(`backend/api/models/repositories.py`) is explicitly "external project or
repository linked to an organization" — Sentry projects are already
`Repository` rows with `provider="sentry"`, and the model has a doc
comment anticipating exactly this: *"cross-source mapping (Sentry/Linear
project -> git repo)"*. Model a Jira project the same way:
`Repository(provider="jira", org_id=<jira Organization row>)`, its
tickets as `Issue` rows (`backend/api/models/issues.py`, doc-commented
"Issue from any source (Sentry, Linear, GitHub, etc.)") FK'd to that
repository, and `RepositoryMapping` for the Jira-project → git-repo edge —
the exact same path `db_update_repository_mapping` already uses for
Sentry. This gets you ADR-006's pending-pool query
(`db_get_dispatchable_issues`) and ADR-001's dedup/retry logic for free,
with zero new tables.

The `Credential`/`McpServer` tables (`backend/api/models/connectors.py`)
are a **different, narrower feature** (user-registered MCP servers /
installed marketplace skills, gated to git orgs only) — not the right home
for a source integration's own connection.

## Step 3: The Connection Endpoint

Two genuinely different onboarding shapes exist — pick the one that
matches the source's actual auth model, don't default to GitHub's:

- **Token-paste, per tenant** (`POST /sources/sentry`,
  `backend/api/routers/sources/sentry/route.py`) — this is the template
  for Jira (API token or per-tenant OAuth app, no App-install concept).
  It validates the token by calling the provider directly, encrypts it,
  finds-or-creates the `Organization` row by `(provider, external_org_id)`,
  and publishes a `sync_projects`-style message so project sync runs in
  the background rather than blocking the request. Copy this shape into
  `backend/api/routers/sources/jira/route.py`.
- **App-install redirect** (GitHub) — no backend "connect" endpoint at
  all; the tenant is sent straight to a static
  `github.com/apps/<name>/installations/new` URL, and linking happens via
  an `installation.created` **webhook** creating an orphaned
  `Organization` row that the frontend later claims
  (`POST /organizations/claim`). Only relevant if the new source has an
  equivalent "install a first-party app" concept — Jira does not.

**Wiring, not auto-discovery**: `backend/api/routers/sources/__init__.py`
manually `include_router()`s each provider's router — a new
`sources/jira/` package is invisible to both the running app
(`WebServerPlugin._discover_routers`) and `make generate-types`
(`backend/generate_openapi.py`) unless it's added there explicitly. Both
of those only scan **one level deep** under `api/routers/`.

## Step 4: Webhooks (if the source has them)

Not a shared dispatcher — each provider's webhook router is fully
bespoke, sharing only "verify → publish the raw body onto a FastStream
stream." Aggregation is manual too:
`backend/api/routers/webhooks/__init__.py` explicitly `include_router()`s
`sentry`/`github`/`gitlab` — a Jira webhook module needs the same
treatment (`from .jira import router as jira_router` +
`router.include_router(jira_router, prefix="/webhooks")`), for the same
one-level-deep-discovery reason as Step 3.

Verification is source-specific — pick based on what the source actually
sends, don't assume HMAC. **Fetch the provider's current webhook
documentation before writing the verifier or its test fixtures — the
exact header name, signature algorithm, and payload shape are not things
to reconstruct from memory, and a plausible-but-wrong guess (e.g.
assuming HMAC when the provider uses a bearer token, or the wrong header
casing) passes code review and then silently drops every real webhook in
production.** Existing patterns for calibration, each verified against
that provider's real behavior, not assumed:
- **Sentry**: HMAC-SHA256 over the raw body with a *per-org* secret
  (`client_secret_encrypted`), fast-pathed by an installation UUID in the
  payload with a brute-force-by-org fallback
  (`backend/api/routers/webhooks/sentry/dependency.py`).
- **GitHub**: `X-Hub-Signature-256` HMAC with one shared app-wide secret.
- **GitLab**: plain token comparison (`X-Gitlab-Token`), no HMAC.

Jira Cloud's own webhook behavior (signed or not, what header/param it
uses, whether an installation id is present in the payload) must be
looked up in Jira's current documentation before choosing a verification
strategy — don't extrapolate from the three patterns above.

Stream naming convention: `jeanclode.events.<provider>.<event-family>`
(e.g. `jeanclode.events.jira.issues`, `jeanclode.events.jira.installation`).
Each new stream needs a `consumers:` entry in `backend/configs/docker.yaml`
(and `kubernetes.yaml`) pointing at a FastStream `RedisRouter`:

```yaml
- topic: jeanclode.events.jira.issues
  handler: "api.routers.webhooks.jira.consumer:router"
```

## Step 5: Dispatch — match the model to the source's fix-dispatch shape (add-source.md)

- **Batch polling** (`backend/api/plugins/sentry/dispatch.py`,
  `SentryDispatcher`) — a standing `asyncio` loop, ticking every
  `interval_seconds`. This is specifically for **error-monitoring
  sources** (Sentry, and the same pattern for a future
  Rollbar/Bugsnag/Datadog-Error-Tracking/Honeybadger addition), where many
  machine-generated events plausibly share one root cause and benefit
  from windowing before a fix is attempted. Per ADR-006: partitions
  eligible work by `(source_org_id, git_org_id)`, claims a per-**git**-org
  time window (the git org is the concurrency perimeter — one fix batch
  per git org at a time, checked via
  `db_git_org_has_running_fix_execution`), grabs a batch with
  `db_get_dispatchable_issues` (`FOR UPDATE SKIP LOCKED`), launches a
  container. Started conditionally inside the plugin's own `startup()`.
  **Don't use this model for a discrete-ticket source** (Jira, Linear) —
  batching unrelated human-authored tickets into one PR window is the
  wrong behavior, not just unnecessary complexity.
- **Event-reactive** (`backend/api/plugins/github/dispatch.py`,
  `evaluate_trigger()`) — not a loop at all, a pure function called from
  webhook consumers, firing per item (a `jeanclode:<workflow>` label being
  added, or — for a discrete-ticket source with webhooks — a ticket
  update event). This is the right shape for Jira/Linear: each ticket
  dispatches on its own, straight into `issue_resolve` (add-source.md
  Path A), the same way a labeled GitHub/GitLab issue does.
- **Polling for discovery, not batching** — if a discrete-ticket source's
  webhook tier isn't reliably available, a polling loop can substitute
  for *ingestion* (finding new/updated tickets to create `Issue` rows
  for), but it still dispatches each ticket individually once found —
  it's a webhook replacement, not a `SentryDispatcher`-style batch window.

Whichever dispatch model applies, ADR-006's pending-pool read
(`db_get_dispatchable_issues`) and the git-org concurrency perimeter
(`db_git_org_has_running_fix_execution`) are already provider-agnostic in
intent — a new dispatcher just needs to call them rather than
special-case around them. Only add a `<Name>OrgSettings` with
`batch_window`/`batch_size`/`gate_on_open_fix_prs` (mirroring
`SentryOrgSettings`) for a genuine batch source — a discrete-ticket
source's settings model has no batching fields at all.

## Step 6: `make generate-types`

`backend/generate_openapi.py` builds a standalone app by scanning
`api/routers/` **one level deep** and including any module's `router`
attribute — the same scan `WebServerPlugin._discover_routers` does for the
real app. A new router two levels down (`sources/jira/route.py`) only
appears in the OpenAPI schema (and therefore `packages/api-types`) if it's
re-exported at the one-level-deep package (`sources/__init__.py`,
`webhooks/__init__.py`) — see Steps 3–4. Set `operation_id=` on each route
consistently with existing naming (`link_sentry_source`, `sentry_webhook`
→ `link_jira_source`, `jira_webhook`) since that becomes the generated
TS function name. Run `make generate-types` after adding routers, before
touching the frontend (add-frontend.md).

## ADR constraints worth reading before starting

- **ADR-001** (issue processing decision logic) — dedup/retry/triage-cache
  logic is generic over `Issue`, so a Jira source gets it for free by
  going through the `Issue` table. No Sentry-style "regression via release
  tag" signal exists for Jira; that's fine, it isn't implemented
  generically for Sentry either.
- **ADR-003** (git provider auth) — unrelated to Jira's own auth, but the
  fixer container still needs git credentials for whatever repo the Jira
  project maps to, resolved exactly like Sentry does today. No new git-auth
  work, just add Jira's own credential as an `UpstreamCredential`
  (sandbox-proxy.md) alongside it.
- **ADR-005** (tenant onboarding & project mapping) — the three-step
  onboarding (connect git → connect source → map projects to repos)
  applies directly; Jira has no native code-mapping API so the mapping
  step likely goes straight to name-based auto-resolve + manual
  correction, same upsert path (`db_update_repository_mapping`).
- **ADR-006** (pending pool & batch dispatch) — the pending pool is a
  derived read over `Issue`/`Execution`, already provider-agnostic; Jira
  issues are pending-pool-eligible the moment they're `Issue` rows, no new
  infra needed there.

## Checklist

- [ ] `<Name>PluginConfig` + `<Name>Plugin(BaseHttpPlugin)` created,
      following `SentryPlugin`'s shape (client + dispatch + watch, not a
      thin watcher wrapper)
- [ ] Added to `BUILTIN_PLUGINS` in `api/app.py`; typed `Application`
      property added if needed
- [ ] `Provider` enum extended (`organizations.py`)
- [ ] `<Name>OrgSettings` added to `settings.py` and registered in
      `_SETTINGS_BY_PROVIDER` + the `OrgSettings` discriminated union
- [ ] Source's "projects" modeled as `Repository` rows
      (`provider="<name>"`), tickets as `Issue` rows, mapping via
      `RepositoryMapping` — no new tables invented
- [ ] Connection endpoint under `api/routers/sources/<name>/`, wired into
      `sources/__init__.py`
- [ ] Webhook router (if applicable) under `api/routers/webhooks/<name>/`,
      wired into `webhooks/__init__.py`, with source-appropriate signature
      verification (don't assume HMAC)
- [ ] Webhook signature scheme, header names, and payload shape verified
      against the provider's current official documentation (fetched and
      cited) — not reconstructed from memory or extrapolated from another
      provider's pattern
- [ ] Webhook test fixtures built from a real or docs-verified sample
      payload, not an invented one
- [ ] Consumer(s) registered in `docker.yaml` / `kubernetes.yaml` under
      `consumers:`
- [ ] Dispatch model chosen deliberately (polling vs event-reactive), and
      any Sentry-specific filter it reuses (e.g.
      `db_git_org_has_running_fix_execution`) generalized rather than
      copy-pasted unchanged
- [ ] `operation_id` set on new routes; `make generate-types` run
- [ ] `make qa` passes
- [ ] `make test` passes
