# ADR-006: Pending Pool and Batch Dispatch

## Status

Accepted

## Context

ADR-004 defines the ingestion pipeline: webhooks land in a Redis queue, consumers apply the ADR-001 decision tree, and issues that pass are "pushed to the pending pool." ADR-002 defines the batch processing model: pending issues for a partition are collected on a timed window and dispatched to a container as a batch.

This ADR defines what the pending pool actually is, how the dispatcher reads from it, how it partitions work, and how concurrent dispatchers avoid conflicts.

## Decision: Postgres as the Pending Pool

The pending pool is not a separate data structure. It is a derived property of the issue rows in Postgres.

When the ADR-004 consumer decides an issue should be processed, it upserts the issue row. That's the "push to the pending pool." No Redis set, no secondary queue.

"Pending" is computed at read time by `db_get_dispatchable_issues`: an issue is dispatchable when its repository is mapped to an **enabled** git target, it has no non-`FAILED` fix execution, and its count of `FAILED` fix executions is under the retry cap. `Issue.status` holds `unresolved` / `resolved` / `closed` — it mirrors the provider's own view of the issue and is never a queue state. (The dashboard's "pending" / "running" labels are rendered from the same derivation, in `backend/api/routers/issues/utils.py`.)

Deriving it means there is no state machine to get stuck in. An execution row appearing or failing is what changes an issue's eligibility, so there's no separate write that can be missed on a crash and no reconciliation job to put the two back in agreement.

## Partitioning: one batch per `(Sentry org, git org)` pair

A Sentry org holds many Sentry projects; each maps 1:1 to a git repo; those git repos belong to one or more **git orgs** (a GitHub org / installation, or a GitLab group). The dispatcher partitions its work by the git org that owns the mapped target repo, because a container is handed exactly one git-platform token (`add_git_platform_to_inputs(git_org_id=...)`) and a batch therefore cannot span two git orgs.

Each `(sentry_org, git_org)` pair is an independent batch:

- Its **window** is the Sentry org's `batch_window` setting, claimed on the **git** org's `Organization.last_dispatched_at` column.
- Its **batch size** is the Sentry org's `batch_size` setting.
- It has its own optional merge gate.

Reusing `last_dispatched_at` on the git-org row is safe because a backend rule forbids one git org from receiving batches from more than one Sentry org — the git-org row therefore has exactly one window to track.

Example: one Sentry org whose projects map into `orgA{repoA, repoB}` and `orgB{repoC, repoD}` produces two partitions — `{repoA, repoB}` and `{repoC, repoD}` — each on its own window and gate.

## Per-Sentry-org settings

Two settings on `SentryOrgSettings` (JSONB on the Organization row) tune batching, both editable on the integrations page:

- **`batch_window`** — `5m` (default) / `1h` / `12h` / `1d` / `3d` / `1w`. A longer window lets more related errors accumulate into one PR (ADR-002's cascade benefit) at the cost of dispatch latency. Resolved to minutes via `BATCH_WINDOW_MINUTES` (kept next to the enum so the SQL and the Pydantic default can't drift).
- **`batch_size`** — `1` / `3` / `5` (default) / `10`. The `LIMIT` on the batch selection query. Falls back to the dispatcher's config-level `batch_size` when unset.

## Dispatcher Plugin

The dispatcher is its own plugin; it depends only on the database plugin and the container plugin, and can be deployed standalone as a dedicated dispatch worker.

It runs a background `asyncio` loop that ticks every 30 seconds. On each tick:

1. **Find eligible pairs** — `db_get_eligible_dispatch_targets` returns `(sentry_org_id, git_org_id)` pairs that have ≥1 dispatchable issue, whose git-org window has expired, whose Sentry org has `triggers.triage = automatic`, whose git org has **no fix batch currently running**, and (when the gate is on) whose git org has no open fix PR.
2. **Fan out** — process eligible pairs concurrently via `asyncio.gather` (bounded by `max_concurrent_dispatches`). Different git orgs dispatch in parallel.
3. **Per pair** — re-check the running-batch condition, reconcile open fix PRs (if gated), atomically claim the git-org window, grab a pair-scoped batch, dispatch one container.

### Dispatch Window and Race Safety

Multiple pods can run the dispatcher concurrently. A single atomic query prevents two pods from dispatching for the same git org in the same window:

```sql
UPDATE organizations
SET last_dispatched_at = now()
WHERE id = :git_org_id
  AND (last_dispatched_at IS NULL
       OR last_dispatched_at < now() - make_interval(mins => :window_minutes))
RETURNING id
```

`window_minutes` is resolved from the Sentry org's `batch_window` and passed in. If the statement returns a row, the pod won the window. If not, another pod already dispatched for this git org. `last_dispatched_at` survives restarts — no timer drift, no lost state.

### The git org is the concurrency perimeter

Only one fix batch runs per git org at a time. A fixer container clones repos, runs the multi-agent pipeline and drives CI; a second concurrent fixer on the *same* git org doubles that org's container and LLM load (ADR-010) and races the first on the same repos and branches. Different git orgs are unaffected and dispatch in parallel.

`db_git_org_has_running_fix_execution(git_org_id)` — an EXISTS on any `sentry`-provider `fix` execution in `QUEUED` / `RUNNING` whose linked issues' mapped repo belongs to that git org (a hand-triggered manual fix counts too). It is enforced in three places:

- **Eligibility query** — the pair isn't returned at all.
- **`_dispatch_target`, before the window claim** — re-checked, so a fixer that outlives its `batch_window` doesn't cause the dispatcher to burn the org's window while it runs.
- **`_claim_batch`** — final re-check inside the same transaction that inserts the execution.

No extra lock is needed: `db_claim_dispatch_window` is already the per-git-org mutex across pods (only the pod that wins the window reaches `_claim_batch`), so the re-checks only guard against a batch from an *earlier* window still being in flight.

### Batch Collection

After claiming the window, the dispatcher grabs pending issues scoped to the pair:

```sql
SELECT ... FROM issues
JOIN repositories sentry_repo ON ...
JOIN repository_mappings ON ...
JOIN repositories git_repo ON git_repo.id = repository_mappings.mapped_repo_id
WHERE sentry_repo.org_id = :sentry_org_id
  AND git_repo.org_id    = :git_org_id
  AND git_repo enabled
  AND no non-FAILED fix execution exists for the issue
  AND count(FAILED fix executions) < max_retries
ORDER BY created_at
LIMIT :batch_size
FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` prevents conflicts if another pod races on the same issues. The committed FIX execution — not the lock — is what actually makes the issues undispatchable on the next tick.

### Overflow

Issues beyond the batch cap stay eligible. Once the current fix batch finishes and the git-org window expires, the pair is eligible again and overflow cascades naturally through subsequent windows.

## The merge gate (opt-in)

`SentryOrgSettings.gate_on_open_fix_prs` (default **off**) makes a partition hold its next batch until every PR the previous batch opened has been merged or closed.

- **Marker** — the `execution_pull_requests` link from a `fix` execution to a `pull_requests` row (Part D). Never a branch name or title search. A group's fix can open one PR per target repo; all of them are tracked.
- **Gate** — in the eligibility query and re-checked in `_claim_batch`, a `(sentry_org, git_org)` pair is excluded when any FIX-linked PR under that git org has `state = 'open'`. Closed counts as done. There is no age escape hatch — a forgotten open bot PR halts that git org's Sentry fixes until someone deals with it.
- **Active reconciliation** — before a gated dispatch, `reconcile_open_fix_prs` polls the provider for every PR the DB still believes is open under that git org and writes back any change (emitting the same SSE the webhook would). This repairs a gate stuck closed by a dropped PR/MR webhook. Terminal rows are never re-polled; a provider error falls back to the last-known DB state and the tick proceeds; a definitive 404 marks the PR closed.

## Failure and Re-insertion

When a container completes, it reports outcomes:

- **Triage: not actionable** → issue's computed status becomes `not_actionable`.
- **Success: PR opened** → the PR is persisted and linked to the execution (Part D); the issue's computed status follows the PR (`pr_open` → `pr_merged` / `rejected`).
- **Phase 1 failure (crash during triage)** → the execution lands in `FAILED`, which makes the issue eligible again. Re-enters the pool.
- **Phase 2 failure (fix crashed)** → same: `FAILED`, so the issue is eligible again up to the retry cap. Re-triaging is cheap, and the container reuses the existing branch and PR rather than opening a duplicate. If the gate is on, the leftover open PR blocks re-dispatch until it's resolved — desirable, not a bug.

Retry counter is capped at 3 attempts per ADR-001. After that, the issue is permanently failed.

## Consequences

- **No separate queue for pending issues.** Postgres is the single source of truth.
- **Dispatcher is a standalone plugin.** Depends only on database and container plugins. Deployable independently.
- **Work partitions by git org.** Each `(sentry_org, git_org)` pair batches independently — its own window, batch size and gate — because a container carries exactly one git-platform token.
- **One fix batch per git org.** A running fixer holds only its own git org's next batch (`db_git_org_has_running_fix_execution`), so a busy org can't overwhelm its own repos or credentials while other orgs keep moving.
- **Race-safe.** Atomic `last_dispatched_at` update per git org (the per-org mutex across pods); `FOR UPDATE SKIP LOCKED` on issue selection.
- **Restart-safe.** `last_dispatched_at` is persisted. No timer drift after restarts.
- **Simple polling.** 30-second ticks, one query for eligible pairs, concurrent fan-out. No signals, no pub/sub.
- **Failures re-enter the pool.** Both phase 1 and phase 2 failures make the issue eligible again with a retry counter. No special retry paths.
- **Optional merge gate.** A tenant can require every fix PR to land before the next batch, backed by the definite execution↔PR link and a webhook-loss backstop.
