# Jeanclode

Self-hosted autonomous coding agent. Listens to Sentry errors, GitHub, and GitLab events, then runs purpose-built multi-agent workflows on the Claude Agent SDK to triage and fix issues — opening PRs automatically without human intervention.

## Project Structure

- `backend/` - FastAPI backend — webhook ingestion, queue dispatch, tenant management, real-time SSE
- `cli/` - Python CLI (`jeanclode`) — thin runner that drives the multi-agent pipeline via Claude Agent SDK
- `security-proxy/` - Sidecar that injects credentials into agent HTTP traffic without exposing them to the subprocess
- `frontend/` - Nuxt 4 dashboard — workspace, integrations, live execution feed, leaderboards
- `packages/api-types/` - Shared TypeScript SDK generated from the backend OpenAPI schema
- `website/` - Nuxt marketing site + docs and blog (`website/content/`)

Workflows live under `cli/src/workflows/`:
- `sentry_fix/` — multi-phase pipeline: fetch → parallel triage (triage *is* the planner: it picks the target repo(s) and writes the fix plan) → synthesis (grouping only, so one root cause is one MR) → fix → CI-gate and label; a group's fix can span several repos, each with its own worktree and PR (opened ready, never draft) before the fixer runs, on one shared branch. Dispatch (ADR-006) partitions by `(sentry_org, git_org)` — each pair batches on the Sentry org's `batch_window`/`batch_size`, the **git org is the concurrency perimeter** (one fix batch per git org at a time; other git orgs run in parallel), and an opt-in per-org merge gate holds the next batch until the previous one's PRs land. The backend persists each PR the run reports and links it to the execution (`execution_pull_requests`), which drives the issue's dashboard status and the merge gate.
- `code_review/` — 7-agent pipeline: IssueExplorer → 2× Analyzer (parallel) → Synthesizer → Deduplicator → FactChecker → Guardrail → Styler; posts inline comments on GitHub PRs / GitLab MRs. On a bot-opened PR it either posts the `@jeanclode-bot` follow-up sweep (findings remain → another loop round) or, on LGTM, the ready notice that @-mentions the org's notify list — see "Ready notice" below
- `pr_summary/` — Summarizer → Parser, with a File Summarizer started 3s after the Summarizer (it shares the Summarizer's system prompt and output schema so it reads its prompt cache) → writes the PR/MR description plus a collapsed "Changes per file" File | Content table (one line per file; the file list comes from the diff, not the model); the summarizer carries over any link the old description had (Sentry issue, related MR, ticket) since it replaces that description wholesale
- `issue_resolve/` — triage → fix → open PR(s); triage explores the codebase itself and hands findings straight to the fixer (no separate plan stage); supports GitHub issues and GitLab issues/work_items; when a fix needs one or more linked repos beyond the issue's own, each gets a worktree and — once the fixer actually pushes to it — its own PR, opened lazily rather than speculatively. Which repos are available to it is resolved by `related_repos` (see backend/CLAUDE.md): repo groups, the org's always-include list, and the subgroup pack
- `jeanclode_respond/` — handles `@jeanclode-bot` mentions; planner either `route`s (defers to an existing pipeline — review/summary/resolve) or `handle`s the ask directly via Bash/git/`gh`/`glab`, judgment-governed rather than an enumerated whitelist. Two deterministic post-turn checks against provider state (never the planner's own account): the branch tip moved → re-attach `jeanclode:review`; nothing pushed but the last open thread closed → the ready notice, since that round of the loop converged with no re-review to come
- `_smoke/` — the `echo` workflow, an internal smoke test (no external API calls)

Every agent runs on the default LLM credential from the pool (ADR-010), except
the fixer in `issue_resolve`: when the pool offers a choice
(a credential on another host, or a `model_heavy` tier), triage can move the
fixer to another credential when a skill or the issue asks for it by name, or
to the heavy tier for clearly heavy work. See "Fixer LLM choice" in
[cli/CLAUDE.md](./cli/CLAUDE.md).

Every agent that reads `.context/diff` sees noise files (lockfiles, minified bundles, assets, snapshots, generated code) as a header plus a `+/-` count stub, never their content — `prepare_diff` in `cli/src/adaptors/diffn.py` owns that list.

## Ready notice

A bot-opened PR/MR runs a loop: review posts findings → the `@jeanclode-bot`
follow-up sweep dispatches respond → respond fixes and pushes (which
re-attaches `jeanclode:review`) → review runs again. It exits when review
finds nothing and posts LGTM.

Once that loop converges, the people a tenant picked in the git integration
page (org settings → `notify.on_ready`, stored as provider identity ids) get
@-mentioned in a comment. Two places emit it, because the loop has two exits:

* `code_review` on LGTM — the review came back clean.
* `jeanclode_respond` when a turn pushed nothing but resolved the last open
  thread — every finding was a false positive, so no push, no relabel, no
  re-review, and LGTM would never arrive.

Both go through `post_ready_notice`, which is post-once per PR/MR via an
HTML-comment marker (`src/runtime/notify.py`) so whichever exit fires second
stands down. It is a comment and not the description on purpose: `pr_summary`
rewrites descriptions wholesale, so mentions placed there don't survive.

Handles reach the container as `JEANCLODE_NOTIFY_USERS` (JSON array), resolved
backend-side by `add_notify_to_inputs` from identity ids against the org
chain's memberships — so a rename can't stale out and someone removed from
the org stops being notified. No env var is the off switch.

## Skills

- `/add-integration` — Add a new source (Linear, GitHub Issues) or workflow (code review, dependency update). Run: `/add-integration source linear` or `/add-integration workflow code-review`

## Architecture Decision Records

Read these before working on any issue — they define the full system design:

- [ADR-001: Issue Processing Decision Logic](./docs/adr/001-issue-processing-decision-logic.md)
- [ADR-002: Cascade and Batch Processing](./docs/adr/002-cascade-and-batch-processing.md)
- [ADR-003: Git Provider Authentication](./docs/adr/003-git-provider-authentication.md)
- [ADR-004: Ingestion Pipeline and Scaling](./docs/adr/004-ingestion-pipeline-and-scaling.md)
- [ADR-005: Tenant Onboarding and Project Mapping](./docs/adr/005-tenant-onboarding-and-project-mapping.md)
- [ADR-006: Pending Pool and Batch Dispatch](./docs/adr/006-pending-pool-and-batch-dispatch.md)
- [ADR-007: CLI Workflow and Agent Pipeline](./docs/adr/007-cli-workflow-and-agent-pipeline.md)
- [ADR-008: CI-Gated Fix Verification](./docs/adr/008-ci-gated-fix-verification.md)
- [ADR-009: Agent Memory System](./docs/adr/009-agent-memory-system.md)
- [ADR-010: LLM Credential Failover and Rate Limits](./docs/adr/010-llm-credential-failover-and-rate-limits.md)

## Make Commands

- `make qa` - Run pre-commit checks (linting, formatting)
- `make test` - Run all tests

`make test` takes ~3 minutes, so run it **once** and capture failures in that
same run — never re-run it just to re-read the output:

```
make test 2>&1 | grep -E "^(FAILED|ERROR)|^=+.*(failed|error)"
```

Empty output means everything passed. Anchoring matters: pytest runs verbose,
so an unanchored `failed` also matches passing tests whose *name* contains it
(`test_invoke_emits_failed_end_on_exception PASSED`). `^FAILED` catches the
short-summary lines and `^=+.*failed` catches the totals line.

## Workflow for Implementing Issues

When working on a GitHub issue:

1. Read the issue description and its dependency comments
2. Read the relevant ADRs linked in the issue
3. Read the sub-documentation for the area you're working on
4. Follow existing code style and patterns in the codebase
5. Run `make qa` and `make test` before opening a PR, if the db and redis are not up run them using `make compose-test`
6. Run `make generate-types` before pushing to keep the frontend SDK in sync
7. Branch naming: `feat/{issue-number}-{short-name}` (e.g. `feat/12-triage-agent`)
8. Open a PR linking the issue with `Closes #N` in the description

## Sub-documentation

- [Backend](./backend/CLAUDE.md)
- [CLI](./cli/CLAUDE.md)
- [Frontend](./frontend/CLAUDE.md)
