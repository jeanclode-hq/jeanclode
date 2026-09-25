# ADR-010: Multi-Credential LLM Failover and Rate-Limit Handling

## Status

Accepted — implemented, tracked by [jeanclode-hq/jeanclode#16](https://github.com/jeanclode-hq/jeanclode/issues/16)

Amended by [jeanclode-hq/jeanclode#43](https://github.com/jeanclode-hq/jeanclode/issues/43): the fixer can run on a credential other than the run's default. See "Amendment: fixer LLM choice" below.

## Context

LLM configuration today is a single instance-level credential: one row in
`instance_settings` (category `llm`, see `backend/api/services/instance_settings.py`
and `LLMConfigInput`/`LLMConfigView` in `backend/api/routers/admin/schemas.py`).
The `claude_code` provider already supports authenticating with an OAuth token
from `claude setup-token`, running inference against the operator's own Claude
subscription (Pro / Max / Max ×5) rather than a pay-per-token API key.

Subscription plans are governed by rolling usage windows — a 5-hour window
plus 7-day windows (overall, and separately for Opus- and Sonnet-tier models)
— enforced by Anthropic and distinct from the documented per-organization
RPM/ITPM/OTPM limits that apply to API-key traffic. With a single credential,
once the subscription's window is exhausted every dispatch fails until it
resets, with no fallback and no way to keep working against, say, a
secondary API key in the meantime.

We want to:

1. Configure multiple LLM credentials (API key and/or OAuth subscription),
   ordered by admin-assigned priority.
2. Automatically fail over to the next credential when one is exhausted.
3. When a subscription credential is close to or past its limit, requeue the
   affected work for its next rolling window rather than failing it outright.
4. Do this safely under concurrent dispatch — several containers can be
   using the same credential at once, and none of them individually knows
   the others are also about to exhaust it.

### What we ruled out, and why

- **Proactively estimating usage via a "how much is left" API call.** Claude
  Code's own `/usage` reads cached data sourced from undocumented
  `anthropic-ratelimit-unified-*` response headers and an equally
  undocumented `/api/oauth/usage` endpoint that is unreliable in practice.
  A minimal probe call reading those headers directly does work (verified
  against real accounts by community tooling), but the header contract is
  reverse-engineered, not published by Anthropic, and could change format
  without notice.
- **A self-calibrated token budget per plan tier.** Worse than the header
  approach, not better: Anthropic silently retunes plan limits over time,
  and a locally-hardcoded budget constant has no way to track that. Any
  proactive signal must come from Anthropic live, or not be trusted as
  authoritative.
- **The `claude.ai/api/organizations/{id}/usage` endpoint.** Returns the
  full picture (all rolling windows, overage, credits) but authenticates via
  the `sessionKey` browser session cookie, not the OAuth `user:inference`
  token Claude Code already uses. That's a broader-privilege, harder-to-rotate
  credential than anything else in this system (no refresh-token flow, ~30
  day TTL, human-in-the-browser renewal) — the wrong trust boundary for
  unattended production dispatch.
- **Atomic reservation against a locally-estimated cost-per-workflow
  ledger.** Sound in principle, but adds real complexity (per-bucket
  reservations, calibration, decay) to solve a race whose actual blast
  radius is small: batches are capped at 4-5 issues (ADR-002) and dispatch
  ticks every 30 seconds (ADR-006), so the worst case from *not* solving the
  race up front is a handful of wasted, cheaply-retried attempts — not
  worth the machinery.

Given all of that, this ADR is a **reactive design**: skip credentials
already known to be exhausted, but treat the real, documented `429
rate_limit_error` as the only authoritative signal, and make retrying after
one safe.

## Decision

### Data model

The credential pool is instance-level — one global, admin-configured list
shared across every tenant, the same scope as today's single `llm`
instance-settings row. There is no tenant-scoped credential concept
anywhere in this design.

Replace that single `llm` instance-settings row with a proper table,
`llm_credentials`, since a priority-ordered list with per-row state doesn't
fit the existing flat category/key model:

```
id               uuid primary key
priority         int, unique, admin-assigned (lower = tried first)
kind             'api_key' | 'oauth_subscription'
provider         same enum as today's LLMConfigInput (claude_code, anthropic, openai, openai_compatible)
plan_tier        text, nullable — informational only (pro | max | max_5x), not used for admission control
secret_encrypted bytea — API key or OAuth token, via DatabasePlugin.encrypt, same as instance_settings today
model_high       text
model_low        text
base_url         text, nullable
status           'active' | 'stale'
stale_until      timestamptz, nullable
created_at / updated_at
```

Admin surface mirrors the existing GitHub/LLM config pattern (masked-secret
view model, upsert endpoints) with the addition of reordering (priority is
just an integer the admin UI lets you drag or edit directly).

### Dispatch-time selection

Every dispatch path — Sentry's batch dispatcher and the GitHub/GitLab
webhook-driven and manual-trigger paths for `review`/`summary`/`respond`/
`issue-resolve` — resolves its LLM credential through the same function,
`add_llm_to_inputs` (`backend/api/plugins/container/dispatch_inputs.py`),
called from every `launch_*` in `sentry/launch.py`, `github/launch.py`, and
`gitlab/launch.py`. That's the one place the priority walk needs to live:
walk `llm_credentials` in priority order, skip any row where
`status = 'stale' AND stale_until > now()`, use the first available row.
Every workflow gets this for free without per-workflow wiring.

An optional, non-authoritative pre-filter: before dispatch, a minimal
inference call can read the `anthropic-ratelimit-unified-*` response
headers described in Context to skip a credential already close to its
limit. This is strictly an optimization — if the headers are missing,
malformed, or the format changes, dispatch proceeds as if the check hadn't
happened. Nothing about correctness depends on this signal existing.

### No credential available

This covers exactly one state: **at least one credential is configured, but
every one of them is currently `stale` with `stale_until` in the future.**
It does not cover an empty `llm_credentials` table — see "Misconfiguration"
below, which is a different failure with a different (non-retrying)
response.

What happens on real, temporary exhaustion differs by dispatch path,
because only one of them already has a poller — and it's cheaper to lean on
that than to build one mechanism to cover both:

- **Sentry (batch/pending-pool path).** ADR-006 already ticks every 30
  seconds and re-derives everything from scratch, including which issues
  are `pending`. Nothing new needed: leave the issue `status = 'pending'`
  exactly like any other retryable failure. The next eligible tick tries
  again — cheap, and naturally bounded by whichever credential's
  `stale_until` comes first (the pre-filter above can skip straight to that
  timestamp instead of re-checking every 30 seconds for nothing, but that's
  an optimization, not a requirement).
- **Every other path — GitHub/GitLab webhook-driven and manual triggers**
  (`review`, `summary`, `respond`, `issue-resolve`). These dispatch
  synchronously and immediately off a webhook or API call
  (`launch_container`, `launch_respond_container`, etc.) — there is no
  poller here today. Concretely: an `@jeanclode` mention lands on the
  ingestion Redis queue (ADR-004), a consumer picks it up and calls into
  the respond dispatch path, which finds every credential stale. Nothing
  will come back and re-check this on its own, so the launch function
  marks the execution `status = 'scheduled'` with `retry_at` set to the
  earliest `stale_until` across the stale rows, instead of `FAILED`.
  `scheduled` is a distinct status from `QUEUED` — it does not reuse or
  otherwise touch `QUEUED`'s existing meaning.

  A new, small periodic task — the same shape as ADR-006's tick, but a
  separate one, since it scans a different table for a different reason —
  claims due rows via `SELECT ... WHERE status = 'scheduled' AND
  retry_at <= now() ... FOR UPDATE SKIP LOCKED` (same atomic-claim idiom as
  ADR-006, safe across multiple backend pods), flips their status inside
  that same transaction so no other pod's tick can also claim them, then
  pushes a retry message — just `{execution_id}`, nothing else — onto
  **the same Redis stream the execution originally came from**. The
  `Execution` row already carries `provider`, `workflow`, and its
  `issues`/`pull_requests` relationships (`backend/api/models/executions.py`),
  which is enough for the poller to derive the correct origin stream on its
  own — no new column needed to remember where it came from.

  Every consumer that can produce a `scheduled` execution (`github/gitlab`
  × issue/PR/mention) gets one small addition: if the incoming message
  carries `execution_id`, skip parsing a fresh webhook payload and instead
  load the existing execution and its already-resolved target from the DB
  — then fall through into the **same** decision/trigger/dedup logic and
  the **same** `launch_*` call a fresh webhook already goes through.
  Deliberately not a bypass straight to `launch_*`: that decision logic
  (per-org trigger toggles, dedup, current PR/issue state) can legitimately
  have changed since the original attempt, and a retry should be subject to
  whatever it says today, not to a frozen copy of what it said hours ago
  when the execution was first scheduled. If that logic decides not to
  dispatch on retry — trigger got disabled, PR got closed in the meantime —
  that's correct behavior, and it lands in whatever terminal outcome the
  workflow already produces for "not actionable" today; no new status is
  needed for it.

  `scheduled`/`retry_at` has two writers, not one: `launch_*` itself writes
  it at admission time (as just described); the watcher writes it a second
  way, when a container it's already dispatched hits a 429 mid-run (see
  "Detecting real exhaustion" below) — the watcher can only react to
  failures from containers that exist, so it can't be the one covering the
  admission-time case where nothing was ever dispatched. Both writers land
  on the same shape and the poller doesn't care which one wrote it.

### Misconfiguration: zero credentials configured

An empty `llm_credentials` table is not exhaustion and must not enter the
retry loop above — there is no `stale_until` to compute a `retry_at` from,
and nothing about waiting will ever fix it; it needs an admin to add a
credential. `launch_*` treats this exactly like today's existing
"no watcher available" case: mark the execution `FAILED` immediately with
`error_type="no_llm_credentials_configured"`, the same
`db_update_execution_status` call already used for that failure mode.

### Cancelling a scheduled execution

`scheduled` is cancellable; `QUEUED` is not touched by this design and
stays whatever it means today. Cancelling a `scheduled` execution needs no
container backend call — nothing has been dispatched yet, so it's a single
conditional update:

```sql
UPDATE executions SET status = 'cancelled'
WHERE id = $1 AND status = 'scheduled'
RETURNING id
```

Conditional, not a bare write, because the poller could claim the same row
for redispatch at the same instant — if the update returns no row, the
poller won that race a moment earlier and cancellation arrived too late,
which should be reported as such rather than silently treated as success.
Cancelling an already-`running` execution is a different, larger problem —
it requires reaching into the container backend (`ContainerBackend.stop_container`,
`backend/api/plugins/container/backend.py`) to kill a real container, which
no endpoint exposes today. That's pre-existing scope unrelated to
credentials specifically, not something this ADR needs to solve.

### Status comment

The existing sticky status comment (`backend/api/plugins/container/status_comment.py`)
buckets each workflow's latest execution into completed/running/errors and
upserts one marker-based comment per PR/issue. It has no bucket for
`scheduled` today — and it shouldn't go under "errors", since that reads as
terminal/failed, which this isn't. Add one new bucket, rendered from the
row's own `retry_at` (not a separately-stored string, so it can't go
stale):

> ⏳ **Deferred**: Respond — rate limited, retrying at `2026-08-27 23:10 UTC`.
> Want to cancel? See your dashboard.

The comment only tells the user to go to the dashboard — it doesn't carry
its own cancel action.

### Detecting real exhaustion

Detection and every state mutation belong to the **watcher** — the existing
backend plugin that dispatched the container and already tails its log
stream (`GitHubPlugin` / `GitLabPlugin` / `SentryPlugin`, per
`backend/CLAUDE.md`). These already have DB access and already hold the git
provider credentials (ADR-003), which is exactly what's needed here:

1. **The container must catch the 429 itself and log it properly.** The CLI
   already emits structured JSON logs in container mode (`cli/CLAUDE.md`).
   Wherever the Agent SDK/HTTP call surfaces a `rate_limit_error`, the CLI
   catches it as a distinct, identifiable error (not folded into a generic
   exception log line) and emits a structured event carrying: which
   credential was in use, the workflow and unit of work, `retry-after` if
   present, and — for `sentry_fix` specifically — the branch and PR URL
   that were in scope for the group that failed, since those are only
   known inside the container at the moment of failure.
2. **The watcher parses that event off the log stream** (the same stream it
   already consumes, no new transport). On seeing it:
   - marks the corresponding `llm_credentials` row `status = 'stale'`,
     `stale_until` from `retry-after` when present; otherwise **5 hours**
     — the shorter of the two known Anthropic window sizes (5h / 7d). A
     wrong guess this way is cheap: the credential gets tried again,
     produces one more 429, and `stale_until` is corrected from that
     response. Guessing the 7-day window instead and being wrong would
     idle a working credential for a week for no reason — the asymmetry
     is why short is the safer default, not long.
   - flags the execution for retry.
3. **For `sentry_fix`, the watcher also performs the leftover-PR cleanup
   itself**, using the git provider credentials it already holds — see
   below for why this shouldn't be left to in-container code.

### Retry after a mid-run failure

This is the post-dispatch case — a container already started, then hit a
429 partway through. The execution goes back through the same two paths as
"No credential available" above (`pending` for Sentry, `scheduled` +
`retry_at` for everything else), which naturally re-runs credential
selection from scratch and lands on the next available credential, or
waits again if none are left.

This only works because every workflow today restarts from its own
beginning on retry — there is no mid-pipeline resume anywhere in the
system yet. That makes "is a full restart safe" the operative question per
workflow:

- **`issue_resolve`** — already safe by design. PR creation is lazy (only
  after `verify_fix_pushed` confirms a real commit), and the runner's own
  docstring states the restart-at-triage behavior is deliberate.
- **`sentry_fix`** — not safe on its own; the watcher cleanup below is what
  makes it safe.
  `_run_group` (`cli/src/workflows/sentry_fix/runner.py`) opens a PR per
  target repo *before* the fixer agent runs.
  `branch_name`/`push_branch`/`open_pr`
  are deliberately built to reuse that PR/branch on a retry of the same
  group — but a full-workflow restart re-triages first, and
  `TriageAgent` searches `gh pr list --state all --search ...`
  (`cli/src/prompts/sentry/triage.md`) — `--state all` includes closed PRs
  — matching on title *or body*. The leftover PR's body contains the
  literal Sentry issue URLs and root-cause text (`pr_body()` in
  `cli/src/workflows/sentry_fix/utils.py`), which reads as an exact match.
  `filter_and_route` then drops the issue as already-handled
  (`existing_pr_url` set), and the run reports `status="success"` with the
  bug never actually fixed and an empty PR as the only trace.

  **Required change:** on seeing the rate-limit log event for a
  `sentry_fix` run, **the watcher** (not in-container code) — using the
  branch/PR URL carried in that event, and the git provider credentials it
  already holds per ADR-003 —
  - closes the PR/MR,
  - deletes its branch (hygiene; not load-bearing since `push_branch -f`
    would overwrite it anyway),
  - overwrites **both** the PR title and body with generic, non-identifying
    text (title alone is not sufficient — the match criteria is title *or*
    body, and the body is what actually carries the fingerprint).

  This is deliberately done by the watcher rather than in `_run_group`'s
  own exception handler: a rate-limit failure can also arrive as a
  catastrophic container death (OOM, hard timeout, kill) that never reaches
  an in-process `except` block at all. The watcher runs independently of
  how the container exited, as long as the failure got logged before it
  died — making it the reliable backstop rather than a best-effort
  in-process cleanup.
- **`code_review`, `pr_summary`, `jeanclode_respond`** — reviewed and
  confirmed already restart-safe; not re-audited line-by-line in this ADR,
  worth a spot-check before rollout.

### Reusing the execution id in Kubernetes

A redispatch keeps the same `Execution` row, and therefore the same
execution id. The Kubernetes backend derives every resource name it
creates from that id — the Job (`jc-{plugin}-{execution_id}`) and, from
the Job name, the agent Secret, the proxy Secret, the CA Secret and the
proxy ConfigMap (`_ResourceNames.derive`). So a retry does not get a fresh
namespace to build in: it lands on top of whatever the previous attempt
left behind, and every one of those creates returns `409 Conflict`.

Ownership alone does not save this. Children are tied to the Job by
`ownerReference` so they cascade-delete, but that only fires once the Job
is deleted — and the Job's own deletion is not guaranteed to have happened:

- with `cleanup_jobs: false` (the dev default, kept so finished Jobs stay
  inspectable) nothing is ever deleted, so the whole name family is still
  occupied;
- with `cleanup_jobs: true` the Job is deleted with Background
  propagation, which returns before the garbage collector has reaped the
  children — they can briefly outlive their owner;
- a child that was created but never adopted (the `_adopt` patch failed
  after the create succeeded) has no owner at all, and is orphaned
  permanently.

**Decision: the launch path purges before it creates.** `start_container`
deletes the previous generation of the name family first, rather than
making names unique per attempt. Names stay a pure function of the
execution id, which keeps the Job trivially greppable from an execution id
and keeps the watcher's `container_id` stable across retries.

The Job is deleted with **Foreground** propagation and the launch waits
for it to actually disappear. Foreground is what makes the wait meaningful:
the API server holds the Job object behind a finalizer until its pods and
owned children are gone, so the Job's disappearance is the signal that the
entire family is free — and it is also what deletes the old pod, including
one still running because the previous attempt died without being reaped.
Any child that survives that (the orphan cases above) is then swept by
name. Every step is best-effort: a failed delete is logged and the create
is left to surface the conflict, exactly as it did before.

Deleting the old Job publishes a `DELETED` watch event naming a Job the
retry is about to replace, and the watcher's `DELETED` handler marks the
execution failed. Job names are reused across attempts but Job UIDs are
not, so the launch records the UID it created and the handler ignores
events from a superseded generation. An unattributable event (UID never
recorded, expired, Redis unreachable) is treated as current — the guard
only ever suppresses what it can positively identify as stale.

This requires `delete` on `secrets` and `configmaps` in the backend's Role,
alongside the `delete` on `jobs` it already had (your deployment's backend Helm values). Without it the Job purge still works — the cascade
covers the ordinary case — and only the orphan sweep degrades to a logged
warning.

## Consequences

- One new table and a small admin CRUD surface, following the existing
  masked-secret pattern already used for GitHub/GitLab/LLM config.
- Correctness never depends on an undocumented Anthropic contract. The
  unified-headers probe is allowed to break silently and only costs a bit
  of proactive filtering; the real `rate_limit_error` is the only thing
  anything is built to trust.
- This is a reactive, not a preventive, design: concurrent dispatches
  racing the same credential's exhaustion will produce a bounded number of
  wasted attempts (bounded by ADR-002's batch cap) rather than zero. This
  trade is deliberate — see "What we ruled out."
- `sentry_fix` depends on the watcher's close/delete-branch/scrub cleanup
  (`api/plugins/container/rate_limit_cleanup.py`); without it, a rate-limit
  retry silently drops the Sentry issue instead of retrying it.
- The CLI needs to distinguish a rate-limit error from a generic failure in
  its structured JSON logging, and for `sentry_fix` specifically must
  include the branch/PR URL in that log event so the watcher can act on it
  without needing to reconstruct that state itself.
- One genuinely new piece of infrastructure: a `scheduled` execution
  status, a `retry_at` column, a small periodic task to scan for and
  redispatch them, and a matching small addition to every GitHub/GitLab
  consumer (skip payload-parsing, not decision logic, when `execution_id`
  is present). This only covers the GitHub/GitLab webhook-driven and
  manual-trigger paths — Sentry needs none of it, since ADR-006's tick
  already covers the same need.
- `scheduled` executions are cancellable for free (a single conditional
  Postgres update, no container involved) — cancelling a `running`
  execution is a separate, pre-existing gap this doesn't need to close.
- The status comment gains one new bucket for `scheduled` work, rendered
  live from `retry_at` rather than a stored string.
- Reusing the execution id means the Kubernetes launch path is no longer
  purely additive: it deletes the previous attempt's Job and any orphaned
  children before creating. That costs one extra API round trip per
  resource on every dispatch, including first ones, where all of them are
  no-ops.
- The backend's Role needs `delete` on `secrets` and `configmaps`. This is
  the only new cluster permission the retry path introduces.

## Amendment: fixer LLM choice (#43)

Selection above still picks one **default** credential per run, and every
agent runs on it. Two things are added around it:

- Credentials gain an optional `name` and an optional `model_heavy`.
- For sentry-fix and issue-resolve, after picking the default, dispatch also loads every other non-stale
  credential that reaches a *different host*, each under its own secret
  name, and lists them (public data only) in `JEANCLODE_LLM_OPTIONS`.
  Same-host credentials collapse to the first usable one: the proxy injects
  one credential per host, and they serve the same models anyway.

Triage reads that list and may move the fixer, and only the fixer, to
another credential (only when a skill or the issue explicitly asks for it by
name) or to the heavy tier (when the credential has one and the work is
clearly heavy, or a skill or the issue asks). Anything unconfigured keeps the
default, and the run says so.

What stays the same:

- Priority order and staleness still pick the default. Failover can still
  cross providers.
- A stale credential is never offered as an option.
- The 429 event carries the credential of the *session* that hit it: a
  retargeted fixer reports its own credential id, so the watcher stales that
  row and the retry falls back to the default. Every other session reports
  the default, as before.
- When there's no choice to make (one host, no `model_heavy`), nothing is
  emitted and a run is identical to before.

Per-agent choice beyond the fixer, and org-level forcing of a credential,
are out of scope.

## Future work (explicitly out of scope here)

- **Mid-pipeline resume** — retrying only the failed group/phase instead of
  the whole workflow run, avoiding redoing already-succeeded work.
- **Inline transparent failover before the failure surfaces to the
  workflow** — retrying a 429'd request against the next-priority
  credential immediately, rather than requeuing the whole unit of work.
  Would remove most of the need for per-workflow retry-safety audits, but
  changes credential semantics mid-agent-session — a bigger design
  question on its own.
