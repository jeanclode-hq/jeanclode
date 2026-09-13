# ADR-005: Tenant Onboarding, Project Mapping, and Issue Backfill

## Status

Accepted

## Context

Before Jeanclode can process issues for a tenant, it needs three things: access to their git repos, access to their Sentry org, and a mapping between Sentry projects and git repositories. It also needs to backfill existing Sentry issues so the pipeline doesn't start from a blank slate. This ADR defines the onboarding flow that establishes all of this.

## Decision: Three-Step Onboarding

Onboarding is a sequential, three-step flow. Each step can be replayed independently. State is saved between steps, the tenant can leave and come back.

### Step 1: Git Provider Integration

The tenant connects their git provider:

- **GitHub:** Install the Jeanclode GitHub App on their org/repos.
- **GitLab:** Provide group access tokens per ADR-003.

After installation, we sync the list of accessible repositories from the provider's API. This gives us the full set of repos the tenant has granted access to.

This step follows the same async sync pattern used in the predecessor project, webhook-driven updates after the initial sync.

### Step 2: Sentry Integration

The tenant installs the Jeanclode integration on their Sentry org. Sentry's Integration Platform uses an OAuth flow (similar to GitHub Apps):

1. Tenant clicks "Connect Sentry" → OAuth redirect to Sentry → tenant authorizes → redirect back with a code.
2. We exchange the code for a token scoped to their org.
3. Sentry sends an `installation` webhook with a UUID. This UUID is stored as the tenant identifier for all future incoming issue webhooks.
4. We subscribe to issue webhooks through the installation.
5. We sync all Sentry projects for the org (`GET /api/0/organizations/{org}/projects/`).
6. We backfill existing issues for each project in the background (`GET /api/0/projects/{org}/{project}/issues/`). Issues are stored by Sentry project ID, the repo association only matters later at processing time. Each backfilled issue enters the ADR-001 decision tree like any webhook-delivered issue. After backfill, webhooks take over for real-time.

**Backfill Scope:** How much history the backfill imports is a tenant choice, made at connect time and stored on the Organization (`settings.backfill`):

| Scope | Behavior |
|---|---|
| `none` | Import nothing. |
| `7d` | Issues last seen in the last 7 days. |
| `30d` | Issues last seen in the last 30 days (default). |
| `all` | Every unresolved issue, with no cutoff. |

The default preserves the original fixed 30-day window, so orgs connected before the setting existed are unaffected.

A large tenant can carry thousands of unresolved issues, and the pending pool dispatches them over days (ADR-006). Without a bound, onboarding either floods that pool or, with a simple on/off switch, leaves the dashboard empty. A scope gives the tenant the middle ground.

Skipping is a deferral, not an opt-out. The manual trigger (`POST /sources/sentry/backfill`) carries an explicit scope on its message and runs regardless of the stored preference, so a tenant who declined at onboarding can import later from the integration settings. This matters because the webhook subscription only delivers `created`, `regression`, `resolved`, and `unresolved` actions: an already-known error that keeps firing produces no new webhook, so declining the backfill leaves those issues invisible until they regress.

**Backfill is queued once per connect,** chained off project sync. It is deliberately *not* chained off mapping resolution as well: issues are stored by Sentry project rather than by repo, so the import doesn't depend on mappings existing, and queueing it twice only re-walked the Sentry API for issues that were already imported.

### Step 3: Project-to-Repo Mapping

This is the core of the onboarding. The goal: map each Sentry project to a git repository, automatically where possible, with manual correction where needed.

**Resolution Strategy (executed in order):**

1. **Sentry Code Mappings**, Pull from `GET /api/0/organizations/{org}/code-mappings/`. If a Sentry project has a code mapping, extract the repo and store the association. This is the most reliable source, it's explicitly configured by the tenant in Sentry. Requires Sentry Team plan or higher.

2. **Automatic Name Resolution**, For projects without code mappings, attempt to match Sentry project slugs/names against repo names. This can use simple string matching or a cheap AI model to handle fuzzy cases (e.g., `user-auth-service` ↔ `auth-service`, `billing-api` ↔ `billing`). The exact matching strategy is an implementation detail.

3. **Manual Correction**, Present the results to the tenant: a list of Sentry projects alongside their resolved (or unresolved) repo associations. The tenant can:
   - Confirm correct mappings.
   - Fix incorrect mappings.
   - Leave projects unmapped, that's fine. Unmapped projects simply won't have their issues processed.

**Mapping Granularity:**

Mappings are repo-level only. Multiple Sentry projects can map to the same repo (monorepo case). Sub-path mapping is unnecessary, the CLI agent pulls the full repo and uses stack trace file paths to locate the relevant code.

**Re-sync Behavior:**

Step 3 can be replayed at any time (e.g., after adding new repos or Sentry projects). When replayed:

- The resolution strategy runs only for projects that don't already have a mapping.
- **Manual corrections are preserved.** If a tenant previously corrected a mapping, that correction is not overwritten by a new automatic resolution.
- New projects (not previously seen) go through the full resolution strategy.

**Webhook Identity:**

Once onboarding is complete, the Sentry installation UUID is how incoming issue webhooks map to a tenant. The flow is:

```md
issue webhook arrives
  → installation UUID → tenant
  → issue's Sentry project ID → mapped repo (if exists)
  → if no mapping: skip, do not process
```

## Technical Considerations

### Background Processing

Steps 2 and 3 involve background work, syncing projects, backfilling issues, pulling code mappings, running name resolution. This work runs in background consumer tasks, not in the request path.

The UI presents a live-updating view of progress. The tenant can navigate away and come back, state is persisted.

### Scaling

All background work is non-blocking and parallelized:

- Code mapping pulls are a single API call per org.
- Name resolution can run concurrently across all unmapped projects.
- No blocking queue events. The mapping task is dispatched to a worker pool, same as any other background job in the pipeline (ADR-004).

### Step Independence

Each step can be replayed independently:

- **Step 1 replayed:** Re-sync repo list from git provider. New repos become available for mapping.
- **Step 2 replayed:** Re-sync Sentry projects and backfill new issues, subject to the org's backfill scope. New projects become available for mapping.
- **Step 3 replayed:** Re-run resolution with current repo and project lists. Preserve manual corrections.

## Consequences

- **No manual mapping of thousands of projects.** Code mappings and automatic name resolution handle the bulk. Manual correction is only for the exceptions.
- **Unmapped projects are silently skipped.** No errors, no noise. If a tenant doesn't map a project, its issues are ignored.
- **Code mappings are leveraged but not required.** Tenants on Sentry's free plan (no code mappings) can still onboard, name resolution and manual mapping cover them.
- **Manual corrections are durable.** Re-syncing doesn't destroy tenant work.
- **The mapping step is the slowest part of onboarding.** Running it as a background task with a live-updating UI keeps the experience responsive.
- **Monorepos work naturally.** Multiple Sentry projects can point to the same repo. The CLI agent uses stack traces to find the right code within the repo.
- **Existing issues are not lost, but the tenant bounds how many arrive at once.** Backfill catches what the chosen scope covers before the webhook subscription started; after that, webhooks handle new activity. A tenant who narrows or skips the import can widen it later without reconnecting the org.
- **Issues belong to Sentry projects, not repos.** The repo lookup is a processing-time concern, not a storage concern. Issues are stored by Sentry project ID.
