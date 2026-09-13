# ADR-007: CLI Workflow and Agent Pipeline

## Status

Accepted

## Context

The CLI (`jeanclode`) is the entrypoint for all workflows, both for local developer usage and as the container entrypoint in the backend pipeline (ADR-002). It selects the appropriate workflow based on the URL and subcommand, then orchestrates a multi-agent pipeline using the Claude Agent SDK.

Supported workflows: `sentry-fix`, `code-review`, `pr-summary`, `issue-resolve`, `jeanclode-respond`, `echo`. `code-review`, `pr-summary` and `echo` run from either context; `sentry-fix`, `issue-resolve` and `jeanclode-respond` run only as the container entrypoint (`JEANCLODE_CONTAINER_MODE`) — their CI-gate, labeling and review loop depend on the backend's webhook loop finishing what one run starts, which a bare terminal run has no way to receive.

This ADR defines the CLI interface, repo resolution strategy, and the agent pipeline workflow (using Sentry fix as the reference example).

## Decision

### CLI Interface

The CLI accepts one or more Sentry issue URLs, with `--repo` flags to specify which repository each issue belongs to:

```bash
jeanclode issue1 issue2 --repo org/api issue3 --repo org/billing
```

`--repo` applies to all issues that precede it, up to the previous `--repo`. Examples:

```bash
# All three issues in the same repo
jeanclode issue1 issue2 issue3 --repo org/api

# Two in one repo, one in another
jeanclode issue1 issue2 --repo org/api issue3 --repo org/billing

# Single issue, explicit repo
jeanclode issue1 --repo org/api
```

The backend (ADR-005) resolves each issue's repo from Sentry's code mappings before dispatch and passes the result as `--repo`. The CLI trusts it — no Sentry API calls for repo resolution, no prompts.

### Agent Pipeline

The pipeline adapts based on the number of issues:

**Single issue:**

```
triage(+plan) → fix
```

**Multiple issues (batch):**

```
parallel triage(+plan) → synthesis → parallel fix per group
```

### Phase 1: Triage (and planning)

Each issue is triaged independently and concurrently. The triage agent:

- Receives the issue details, stack trace and error context, pre-fetched (it makes no Sentry API calls itself).
- Reads the affected source to confirm the bug exists in the current code.
- Classifies: is this a code bug or infrastructure noise?
- Decides **which repo(s)** the fix belongs in — its working directory plus any repos cloned alongside it — and names them in `target_repos`.
- Checks for existing PRs/MRs that already address the issue.
- Writes `findings`: the fix plan the fixer will implement.
- Outputs a structured `TriageOutput`.

There is no separate planning agent. Triage has to read the code to reach a verdict at all, so `findings` captures that investigation instead of discarding it and paying a second agent to re-derive it from a prose summary.

Its `kind` is one of `proceed`, `not_actionable`, `duplicate` (an open PR/MR covers it), `already_fixed` (a merged one does), or `previously_rejected` (a PR/MR fixing this exact root cause was closed unmerged — a human declined it, so re-proposing is worse than doing nothing). Only `proceed` continues.

### Routing

Routing is deterministic and repo resolution happens here, by **name** against the checkouts on disk — never by a URL supplied by Sentry's code-mappings API or by the model. A Sentry issue URL carries no repo identity, and self-hosted Sentry has no usable code mapping, so a URL-keyed gate drops correct triages and the run reports a clean no-op. An issue triage marked `proceed` whose target repos resolve to nothing is surfaced as a run **error**, not a silent stop.

### Synthesis (batch only)

When 2+ issues are triaged as actionable, the synthesis agent receives all triage results and groups related issues by root cause. A group may contain one or more issues.

The synthesis agent outputs the partition only: which `issue_ids` belong together, and a shared root-cause description. Every other group field — target repos, findings, affected files, confidence — is rebuilt from the triage records it grouped, so a reworded or invented repo name can't reach the runner that opens PRs against it. Issues it omits become their own group rather than being dropped.

### Phase 2: Fix

Each group is processed independently and concurrently. Within a group, for **each** target repo:

1. A worktree is created on the group's single branch (siblings under one parent when there's more than one repo).
2. The placeholder commit is pushed and a **PR/MR is opened, before the fixer runs** — the live feed gets a link immediately, and each repo's CI has a merge request to run pipelines against. It is opened *ready*, never as a draft: a `Draft:` title makes GitLab skip the pipeline under a `workflow: rules` guard like webshop's, and un-drafting is not itself a pipeline trigger, so the MR would sit blocked on a pipeline that could never be created.

Every git and PR command for a repo runs from that repo's own worktree path. `glab mr create` infers its source branch from the checkout it runs in, so a cwd that isn't the worktree for that branch opens an MR from the wrong branch — silently, as a second MR. The source branch is also passed explicitly for the same reason.

Then **one fixer session** runs across all of the group's worktrees, receiving triage's findings and the per-repo paths, PR URLs and CI-bypass markers. Afterwards, per repo: a real pushed commit gets CI checked and review labels attached; a repo the fixer left untouched has its PR closed, since unlike issue-resolve the PR was opened before the fix existed.

### Container Log Streaming

Every agent step streams structured output to container logs. The backend's container watcher (same architecture as the predecessor project) picks up outcomes in real-time:

- Triage results per issue (including target repos)
- Synthesis groupings
- PR creation, per repo
- Fix outcomes per group (success/failure, and per repo: PR URL, PR **number**, provider, head branch, CI outcome)

The per-repo PR number and provider let the backend address and link the PR without parsing its URL or touching the branch name. On the terminal status the backend resolves each PR's git `Repository` (from the group's mapped repo plus its repo group, matched by `web_url`) and writes an `execution_pull_requests` link — the definite marker ADR-006's merge gate and the dashboard's issue status both read.

This gives the backend full visibility into progress without callbacks. Partial success is naturally handled, if one group's fix fails, the watcher sees it independently and the backend re-inserts those issues as `pending` per ADR-006.

## Consequences

- **Container-invoked.** The backend resolves each issue's repo and passes `--repo`; the CLI does no repo resolution of its own.
- **No batch flag needed.** Multiple issues trigger batch behavior automatically. Single issue skips synthesis.
- **A fix can span repos.** Triage names every repo that needs a change; each gets its own worktree, PR and CI gate on one shared branch, and a single fixer session works across them.
- **Repo resolution is by checkout, not by URL.** The backend's `RepositoryMapping` (`--repo`) or Sentry's code mappings decide what gets *cloned*; triage decides which of those checkouts the fix lands in. Nothing downstream depends on a repo URL the model had to produce.
- **Parallel execution within the container.** Triage is parallel, and fixes run in parallel per group. Bounded by the Sentry org's `batch_size` (1/3/5/10, ADR-002). Only one such container runs at a time across the whole deployment (ADR-006).
- **Backend observability via logs.** No special reporting API. The container watcher reads structured log output for every pipeline step.
- **DX is clean.** `jeanclode issue1 issue2 --repo org/api` reads naturally, and needs no interactive prompt since the backend already resolved every repo before dispatch.
