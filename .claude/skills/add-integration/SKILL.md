---
name: add-integration
description: Add a new source (like Linear, GitHub Issues) or a new workflow (like code review, dependency update) to JeanClode. Use when asked to add a new integration, source, adaptor, workflow, or plugin.
disable-model-invocation: true
argument-hint: "[source|workflow] [name]"
---

# Add Integration to JeanClode

You are adding a new integration to JeanClode. There are two types:

- **Source** — a new issue source like Linear, GitHub Issues, PagerDuty ($0 = "source")
- **Workflow** — a new pipeline like code review, dependency update, incident response ($0 = "workflow")

The name of the integration is: **$1**

**There is no plugin-marketplace layer for Jeanclode's own workflows** —
no `plugins/<name>/` directory, no `.claude-plugin/plugin.json`, no
`SKILL.md` orchestrator invoking `${CLAUDE_SKILL_DIR}/scripts/*.py`. The
real structure is plain Python: `cli/src/workflows/` (orchestration) +
`cli/src/agents/` (LLM turns) + `cli/src/prompts/` (prompt text) +
`cli/src/activities/` (deterministic work), plus a real backend plugin per
source and bespoke frontend per provider.

**First, decide what's actually being added — two independent questions:**

1. **Does each item stand alone, or does it need batching?** A Jira
   ticket, a Linear issue, a GitHub/GitLab issue are each one discrete,
   human-authored unit of work — triage it, fix it, open one PR, done. An
   error-monitoring source (Sentry, and the same would apply to Rollbar,
   Bugsnag, Datadog Error Tracking, Honeybadger) emits many
   machine-generated events that often share one root cause, so it needs
   windowed batching, a polling dispatcher, and a synthesis/grouping stage
   before a fix is even attempted. These are genuinely different shapes —
   **most new sources being asked for (Jira, Linear, PagerDuty) are
   discrete-ticket-shaped, not Sentry-shaped.**
2. **Does the source also host git repos** (and therefore need
   review/summary/respond, not just a fix pipeline)? Only GitHub/GitLab
   today; a new source in this category (Bitbucket) is the only case that
   needs the full multi-skill adaptor surface.

These two questions are independent — Jira is discrete-ticket-shaped and
not git-hosting; Sentry is batch-shaped and not git-hosting; a
hypothetical Bitbucket would be discrete-ticket-shaped (each Bitbucket
issue is its own PR, same as GitHub/GitLab) and git-hosting.
[references/add-source.md](references/add-source.md) covers all of this
and flags where each answer changes the implementation.

A new **pipeline** on an existing source (code review, dependency update,
incident response) is a different kind of addition — read
[references/add-workflow.md](references/add-workflow.md).

**The provider's own API/webhook docs are the source of truth — not
trained knowledge.** Auth header names, URL formats, webhook payload
shapes, signature schemes, pagination, and rate limits all vary by
provider, by plan/tier, and change over time. Before writing any of
`auth.py`, `client.py`, `resolver.py`, a webhook signature verifier, or a
test fixture for any of these, fetch and read that provider's current
official API/webhook documentation directly — don't reconstruct the shape
from memory. This applies with equal force to CLI-side code (URL parsing,
API calls) and backend webhook handling: a header name or payload field
that's almost right fails silently until a real request arrives, and
"almost right" is exactly what unverified recall produces. Cite the doc
page/section actually consulted, and build tests from a real or
docs-verified sample payload/response, never an invented one.

**Every integration that dispatches containers needs all three of these,
not just one:**

- Backend: [references/add-backend.md](references/add-backend.md) — a
  real plugin (client + dispatch + watcher together, following
  `SentryPlugin`), an `Organization` row (there is one shared table for
  every provider, not a per-provider table), a connection endpoint, and a
  webhook router if the source has one.
- Sandbox / security-proxy: [references/sandbox-proxy.md](references/sandbox-proxy.md)
  — unconditional. Every external host the container reaches needs an
  `UpstreamCredential` (with injection) or an `extra_hosts` entry
  (allowlist only), or the run is denied at the proxy before it starts.
- Frontend: [references/add-frontend.md](references/add-frontend.md) —
  there is no generic integrations component; each provider's connect
  form and settings panel is bespoke, and several provider unions are
  hardcoded in multiple places that must be updated together. Check
  `frontend/app/components/onboarding/StepConnectors.vue` first — Jira is
  already stubbed there with `comingSoon: true`.

Reference implementations, pick per question 1 above:
- **Discrete-ticket sources** (Jira, Linear, PagerDuty, and GitHub/GitLab
  issues today) → `cli/src/workflows/issue_resolve/`,
  `cli/src/agents/issue/`, `cli/src/activities/issue/`. No batch
  dispatcher, no synthesis stage.
- **Error-monitoring / batch sources** (Sentry, and the same pattern for
  any future Rollbar/Bugsnag/Datadog-Error-Tracking/Honeybadger addition)
  → `cli/src/workflows/sentry_fix/`, `cli/src/agents/sentry/`,
  `cli/src/activities/sentry/`, `backend/api/plugins/sentry/dispatch.py`
  (polling batch dispatcher + synthesis).

Sentry is still the most complete backend/frontend reference regardless
of which shape a new source is: `backend/api/plugins/sentry/` (plugin,
config, dispatch, watcher, launch), `backend/api/routers/sources/sentry/`
(connection endpoint), `backend/api/routers/webhooks/sentry/` (webhook
ingestion), `frontend/app/components/integrations/SentryOverview.vue` and
`SentryOrgDetail.vue`.

After implementing: run `make generate-types` (before touching the
frontend — new backend routers must exist in `packages/api-types` first),
then `make qa` and `make test`.
