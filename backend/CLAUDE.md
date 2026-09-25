# Backend

FastAPI backend with plugin-based architecture.

## Architecture

The backend uses a **plugin system** where each feature is a plugin with startup/shutdown/health-check lifecycle:

| Plugin | Role |
|---|---|
| `DatabasePlugin` | PostgreSQL via SQLAlchemy; handles encryption/decryption of stored tokens |
| `WebServerPlugin` | FastAPI + uvicorn; mounts all routers |
| `FastStreamPlugin` | Redis Stream broker (LPUSH/BRPOP via faststream) for async jobs |
| `ContainerPlugin` | Config holder for Docker/Kubernetes backends |
| `GitHubPlugin` | GitHub App client: mints installation tokens, handles OAuth, dispatches containers, watches log streams |
| `GitLabPlugin` | GitLab API client: verifies tokens, syncs repos, dispatches containers, watches log streams |
| `SentryPlugin` | Sentry API client: fetches orgs/projects/issues, dispatches containers, watches log streams |
| `OAuthPlugin` | GitHub/GitLab OAuth for the frontend login flow |

The `Application` class in `api/app.py` is the central container. Third-party plugins are loadable via `jeanclode.plugins` entry points.

## Webhook Routers

- `POST /webhooks/sentry` — validates HMAC signature, routes to `jeanclode.events.sentry.{installation,webhooks}`
- `POST /webhooks/github` — unified router dispatching by `X-GitHub-Event`: install, repos, repository (create/rename/transfer/delete), pull_request, issues, issue_comment, PR review/review_comment, organization membership
- `POST /webhooks/gitlab` — unified router dispatching on payload shape first: a payload with `event_name` and no `object_kind` is a **lifecycle event** (project, subgroup, member, or anything a system hook reports), routed by `X-Gitlab-Event`; everything else dispatches on `object_kind` (merge_request, issue, note)

### Two payload shapes, one URL

GitLab does not use `object_kind` everywhere. Project, subgroup and member
events — the group-specific families — carry `event_name` and no `object_kind`
at all, so anything dispatching on `object_kind` alone drops all three
silently. `X-Gitlab-Event` then says how far to trust the payload:

| Header | Events | Stream |
|---|---|---|
| `Project Hook` | project_create, project_destroy | `…gitlab.group` |
| `Subgroup Hook` | subgroup_create, subgroup_destroy | `…gitlab.group` |
| `Member Hook` | user_add_to_group, user_remove_from_group, … | `…gitlab.members` |
| `System Hook` | the same, instance-wide, plus group_destroy | `…gitlab.system` |
| everything else | merge_request, issue, note (`object_kind`) | as before |

`group_hook.py` and `system_hook.py` are the two entry points; both delegate
the project work to `projects.py`, since the payload is identical and neither
hook says which connected group it came from. Ownership is therefore proven,
not assumed: the project is fetched with each connected group's token and kept
only if its namespace chain contains that group — a group token can read public
projects instance-wide, so a successful fetch alone means nothing. This matters
most for system hooks, which fire for every project on the instance.

### Keeping the repository list current

A new repo reaches the DB three ways, and all three converge on the same
upsert + open-PR/issue backfill:

1. **GitHub** — `installation_repositories.added` (thin payload: no `html_url`,
   no owner, so the URL is derived and the row repaired by the backfill) and
   `repository.created`, which is what an org-wide installation actually sends.
   The `repository` event is in the App manifest's `default_events`; an App
   created before that has to add it under its webhook subscriptions.
2. **GitLab** — a group webhook with **Project events** enabled (no admin
   rights needed, set per connected group), or one instance-wide **system
   hook** (Admin Area → System Hooks) covering every group at once. Same URL
   and secret token either way.
3. **Manual** — `POST /organizations/{org_id}/sync-repositories` (the "Sync
   repos" button on the org page) re-runs the provider scan the connection ran.
   This is the fallback for a dropped delivery or a group with no hook
   configured, and the only path that needs no webhook at all.

### GitLab Free: project webhooks (`manage_project_webhooks`)

Group webhooks are Premium-only. On GitLab Free the one group token can't
carry a webhook, so issue / note / merge_request events never arrive. The
`manage_project_webhooks` org setting (slider on the GitLab onboarding step and
the org detail page) makes the repo sync create the Jeanclode webhook on every
project it owns — matched by URL so a re-sync doesn't stack duplicates, PUT
back into shape if a tenant edits its events. Needs a token with `api` scope
and Maintainer+ role. The hook URL is `${BACKEND_URL}/webhooks/gitlab`
(`gitlab.webhook_url` overrides it if the endpoint lives elsewhere).

Turning the setting off, or deleting the org, queues
`jeanclode.events.gitlab.project_hook_teardown` — a self-contained message
(encrypted token + project ids, since the org rows may already be gone) whose
consumer deletes our hook from every project. Org deletion returns 200 before
the teardown runs.

Newly created projects still need to be *discovered* first, which on Free means
an instance **system hook** — configured **without merge request events** (the
project hooks already carry those; enabling both delivers every MR event
twice). The system hook is documentation, not something the backend sets up or
checks; likewise "don't also run a group webhook" is left to the operator.

## Trigger System (per-org)

GitHub/GitLab orgs have no trigger-*mode* setting: `review` and `summary`
dispatch only on their `jeanclode:<workflow>` label being added to a PR/MR
(see `api/plugins/github/dispatch.py`), `issue_resolve` only on
`jeanclode:resolve` being added to an issue, and `respond` only on an
`@jeanclode-bot` mention — nothing is ever auto-triggered by a PR/issue
merely opening or a new commit. A label set at creation counts as added: the
`opened`/`open` event dispatches on the labels it carries, since neither
provider documents a separate label event for them (GitLab sends none), and a
duplicate `labeled` delivery is absorbed by the active-execution check.
Manual dispatch (the dashboard "Fix" / "Resolve" buttons) is a separate,
always-available path — it doesn't read these workflows' trigger settings at
all, because there are none.

What *is* configurable per org is who a mention listens to:
`GitOrgSettings.trigger_permission` (`developer_only`, the default, or
`anyone`) mirrors GitHub/GitLab's own "needs write/Developer+ access to
trigger CI from a comment" convention. `handle_mention_event`
(`api/routers/webhooks/github/mentions.py`) and `handle_note_event`
(`api/routers/webhooks/gitlab/notes.py`) resolve it alongside the repo/org
lookup and skip `fetch_collaborator_permission`/`fetch_member_access_level`
+ `is_authorized` entirely when it's `anyone` — the same bypass the bot-sender
check already gets, just for a different reason. Label-driven dispatch
(`review`/`summary`/`issue_resolve`) has no such check of its own regardless
of this setting: it relies on the provider's native permission model for who
can attach a label.

Sentry orgs still have a configurable `triage` trigger (manual vs
automatic) — Sentry issues have no label concept, so the batch dispatcher
(ADR-006) needs this to decide whether to pick an org up at all. The same
`SentryOrgSettings` carries `batch_window` (5m…1w), `batch_size` (1/3/5/10)
and `gate_on_open_fix_prs` (opt-in merge gate). Dispatch partitions by
`(sentry_org, git_org)`: `db_get_eligible_dispatch_targets` returns pairs,
the window is claimed on the **git** org's `last_dispatched_at` (the per-org
mutex across pods), and the **git org is the concurrency perimeter** — one
`fix` batch per git org at a time (`db_git_org_has_running_fix_execution`,
checked in the eligibility query and re-checked before the window claim and
in `_claim_batch`). Different git orgs dispatch concurrently.

Org settings also carry `notify.on_ready` — provider identity ids (not
handles, so a rename can't stale out) for the people to @-mention once a
bot-opened PR/MR converges. `add_notify_to_inputs` resolves them against the
org chain's memberships at dispatch time and hands the container a
`JEANCLODE_NOTIFY_USERS` JSON array; an empty list means no env var, which is
the CLI's off switch. Every failure path there is a no-op — a missing ping
must never cost the tenant the MR.

## Which repos a run clones (`related_repos`)

`issue-resolve`, `sentry-fix` and `respond` clone more than one repo —
review and summary stay single-repo by design. For `respond` the extra
repos are read-only context (the planner's write access stays scoped to
the mention's own repo via its prompt guardrail, not via what's cloned);
for `issue-resolve`/`sentry-fix` they're fix targets like any other. What
joins the workspace is resolved in `api/plugins/container/related.py`, as
a set union of three sources minus the repo the run was triggered on:

1. **Repo groups** (`RepositoryMapping`) — symmetric, hand-made, never capped.
2. **`related_repos.always_include`** — repo ids on the connected group's
   settings. One-directional on purpose: a run on any repo under the org
   pulls these in, a run on one of *them* pulls nothing back. Subtracting the
   primary last is what produces that asymmetry, and it's why this can't be a
   mapping row — `db_get_related_repos` reads both columns.
3. **`related_repos.pack_subgroup`** (default on) — the other repos sitting
   *directly* in the run's own subgroup. Direct members only, and never on
   the connected root itself, where "the subgroup" would mean the whole
   connection. `related_repos.excluded_subgroups` opts individual subgroups
   out.

The pack is the only capped source (`MAX_PACKED_SUBGROUP_REPOS = 30`), because
it is the only one nobody opted into: related repos are cloned **sequentially,
before triage** (`cli/src/runner/preflight.py`) and priced into the `/tmp`
emptyDir (`sizing.py`), so a 250-project subgroup would spend the run's whole
timeout cloning. Over the cap the pack is skipped whole rather than truncated —
an arbitrary 30 of 250 silently omits the repo the fix needed. Disabled repos
are left out of the pack (an automatic net shouldn't drag in something a
tenant turned off); explicit choices ignore that flag.

Settings are read through `db_resolve_org_settings`, since the connected group
is the only org the UI offers and a subgroup's own row is empty.

## LLM Credential Pool (ADR-010)

`llm_credentials` is an instance-level, priority-ordered pool (lower priority
tried first) rather than a single config row. Each row carries its own
provider (`claude_code`, `anthropic`, `openai`, `openai_compatible`),
encrypted secret, an optional `name`, `model_high`/`model_low`, an optional
`model_heavy`, optional `base_url`, and its own
exhaustion state (`status`, `stale_until`).

Every dispatch path resolves through `add_llm_to_inputs`
(`api/plugins/container/dispatch_inputs.py`), which walks the pool in priority
order and skips rows that are `stale` with `stale_until` in the future. The
CLI echoes the chosen row's id back in its structured 429 log event, and the
watcher marks that row stale from the response's `retry-after` (5 hours when
absent — short is the cheap wrong guess).

When every credential is stale, the two dispatch paths diverge:

- **Sentry** leaves the issue `pending`; ADR-006's 30-second tick retries it.
- **Everything else** (webhook-driven and manual `review`/`summary`/`respond`/
  `issue-resolve`) marks the execution `SCHEDULED` with `retry_at` set to the
  earliest `stale_until`. `ScheduledDispatcher`
  (`api/plugins/container/scheduled_dispatch.py`) claims due rows with
  `FOR UPDATE SKIP LOCKED` and republishes `{execution_id}` onto the stream
  the execution originally came from. Consumers that see an `execution_id`
  skip payload parsing but still run the full decision/trigger/dedup logic —
  a retry is subject to what that logic says now, not to a frozen copy.

An empty pool is misconfiguration, not exhaustion: the execution fails
immediately with `error_type="no_llm_credentials_configured"`.

For issue-resolve only (`fixer_options=True`), once the default
credential is picked, `_add_llm_options` also loads every
other non-stale credential on a *different host* (the proxy injects one
credential per host, so same-host rows collapse to the first) under its own
`JEANCLODE_LLM_OPTION_<n>_SECRET`, and describes them in the public
`JEANCLODE_LLM_OPTIONS` (name, models, the env a session sets to target it,
secrets referenced by name only). It's emitted only when there's a real
choice: a second host, or a `model_heavy` on the default. Triage uses it to
move the fixer to another credential or the heavy tier (#43); a failure
building it is logged and never costs the run its default credential.

## Container Execution

Agents run in ephemeral containers (Docker locally, Kubernetes in production) with a **security-proxy sidecar** that injects credentials (LLM API key, git tokens) into outbound HTTP traffic — never exposed to the agent subprocess as environment variables.

On Kubernetes, every resource name is derived from the execution id — the Job (`jc-{plugin}-{execution_id}`) and, from it, the agent/proxy/CA Secrets and the proxy ConfigMap. A retried execution keeps its id (ADR-010), so `start_container` deletes the previous generation before creating: the Job with Foreground propagation, waited on until it is gone, then any orphaned children by name. Anything new added to that name family belongs in `_ResourceNames.children()` so the sweep keeps covering it.

The Docker backend (`docker.py`) is the same model, not a lesser one: the sidecar container (`jc-{plugin}-{execution_id}-proxy`) is started first, the agent (`jc-{plugin}-{execution_id}`) joins its network namespace via `NetworkMode=container:<id>` — the Docker equivalent of a pod's shared loopback, so `HTTPS_PROXY=http://127.0.0.1:8080` reaches mitmdump and nothing else can. Both containers run read-only-rootfs with two `local` volumes each execution: `…-tmp` (agent `/tmp`) and `…-sandbox` (CA cert/key + proxy config, `put_archive`d in via the sidecar — you can't `put_archive` a read-only rootfs but you can a mounted volume — then mounted `:ro` on the agent, `SECURITY_PROXY_CA_DIR`/`SECURITY_PROXY_CONFIG` env pointing the unchanged proxy image at `/sandbox`). Exit/cleanup/reconcile and retry-purge tear the pair + both volumes down together by id-derived name. One gap vs K8s: the `local` driver can't enforce a size cap, so `tmp_size_limit` (from `sizing.py`) is advisory.

## Configuration

Config is loaded from YAML files in `configs/` (e.g., `docker.yaml`). Environment variables are expanded via `!ENV` tags. The `BackendConfig` class in `config.py` validates config.

Set via `JEANCLODE_CONFIG` env var pointing to the YAML file.

## Database sessions and the connection pool

SQLAlchemy is used synchronously everywhere, so a session holds one pooled
connection from its first query until it closes. How many people the dashboard
can serve at once is the pool size divided by what a single request holds — so
two rules:

**A pooled connection is never held across an `await`.** An `async def` must
not take `db: Session = Depends(get_session)`; it does each unit of DB work in
`await run_in_session(fn)`, which runs `fn(db)` on a worker thread with a
session of its own. Otherwise a GitHub call or a container launch in the middle
of the handler keeps a connection checked out for that whole round trip, and
the blocking SQLAlchemy calls run on the event loop, where a saturated pool
freezes every other request for `pool_timeout` — long enough to trip the
liveness probe.

`run_in_session` returns plain data or response models. An ORM instance handed
back outlives its session and raises `DetachedInstanceError` on first access,
so read the fields you need (or build the response) inside `fn`.

**One request, one connection.** A `def` handler takes the session from
`Depends(get_session)` and passes *that* session down —
`verify_org_access_from_body(db, user, org_id)` and
`verify_workspace_access_from_path(db, user, workspace_id)` take it as their
first argument for exactly this reason. Opening a second session mid-handler
doubles what every such request occupies. `tests/routers/test_pool_usage.py`
pins the count at one.

Endpoints also don't fan out per row: `GET /repos` inlines each repo's group
members and `GET /plugins` inlines each install's credential, because a query
per row in the frontend turns one page view into 25 requests.

**The same rule binds consumers, not just handlers.** FastStream consumers and
the reconcile loop share the web server's pool, so one holding a connection
across a container launch or a provider API call is taken straight out of the
dashboard's budget. Webhook handlers are therefore phased — read/write in a
`run_in_session` closure, then publish, then reserve — and `git_dispatch` takes
ids rather than a caller's session for the same reason. Where the launcher
needs ORM instances, the closure `expunge`s eagerly-loaded ones so it reads
them detached (`db.expire_on_commit = False` first if a commit intervenes,
since an expiring commit leaves them needing a reload that can no longer
happen).

An `async def` with no `await` in it should just be `def` — FastAPI already
runs those on a worker thread, and the session then lives exactly as long as
the handler body. `_cap_worker_threads_to_pool` narrows that shared limiter to
`pool_size + max_overflow` at startup, so threads never outnumber connections.

## Testing

Tests use pytest with fixtures from `tests/conftest.py`. Mock external dependencies with `@patch`.

## Guidelines

- imports should always be at the top of the file
- follow existing code style and patterns
- write docstrings for public classes/functions
- add type hints for function signatures
- always use Pydantic models instead of dataclasses
