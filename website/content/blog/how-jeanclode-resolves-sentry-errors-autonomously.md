---
title: "How JeanClode Resolves Sentry Errors Autonomously"
description: "A technical walkthrough of how JeanClode processes Sentry webhooks, batches related errors by git org, and opens CI-verified fix PRs without human intervention: the decision tree, the batching window, and the two-phase container lifecycle."
publishedAt: "2025-06-10"
readingTime: 9
seoTitle: "How JeanClode Fixes Sentry Errors Autonomously"
seoDescription: "How JeanClode turns a Sentry webhook into a CI-verified pull request: the decision tree, the batching window, and the container lifecycle."
---

Every Sentry alert is a question: is this a real bug, and if so, is it worth fixing right now? Answering that at scale, across tenants, across repos, across thousands of daily error events, is the core problem JeanClode exists to solve.

This post walks through the mechanics: how we decide which issues to process, how we batch related errors so they don't turn into duplicate PRs, and how the container pipeline carries triage through to an open pull request.

## What breaks in the naive version

A naive webhook-to-PR system processes every Sentry event as it arrives. That falls over immediately. The same error fires 30,000 times, and you'd open 30,000 PRs. A deploy causes 12 related errors from one root cause, and you'd open 12 PRs duplicating the same work. JeanClode opens a PR, a human closes it, and you'd keep reopening it. A fix merges but Sentry fires again because it hasn't deployed yet, and you'd start a second fix on top of a working one.

Each of those is expensive: in LLM cost, in compute, and in reviewer trust. A system that cries wolf gets ignored.

## The decision tree

Before any LLM work happens, a deterministic check decides whether an issue is eligible at all. An issue is dispatchable when its repo maps to an enabled git target, it has no fix execution beyond a `FAILED` one, and its count of failed attempts is under the retry cap.

That covers most of it in one rule: **one issue, one live attempt.** Never seen before, process it. Already running, has an open PR, or has a merged PR, skip it, because any of those counts as a non-failed execution already on record. The last attempt failed, retry, up to a cap of three, then stop. A human closed the PR, triage classifies that as a rejection and stops permanently, quoting the reviewer's stated reason back rather than re-arguing it next time.

This runs in milliseconds against Postgres before anything touches an LLM, and there's no separate state to get wedged: eligibility is derived at read time from the executions attached to an issue, not written into a queue column that a crash could leave stale.

Two scenarios in [ADR-001](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/001-issue-processing-decision-logic.md) go further than what runs today: an escalating re-triage threshold for high-volume noise, and deployment-aware regression handling that reads a fix's release tag off the incoming error. Both are specified as the target design. Neither is live yet: a not-actionable issue stays not-actionable at any volume, and today a `regression` webhook only updates the issue's stored status. Nothing re-dispatches on it.

## Batching by git org

The decision tree handles one issue at a time. A bad deploy that fires eight related errors at once is a different problem: fixing each independently means eight PRs, all touching overlapping code, all conflicting with each other. The right answer is one PR, after grouping by root cause.

Work is partitioned by `(sentry_org, git_org)` pair, because a container gets exactly one git platform token and can't span two git orgs in one run. Each pair batches on the Sentry org's own `batch_window` setting (5 minutes by default, configurable up to a week) and `batch_size` (5 issues by default). The git org, not the tenant, is the concurrency perimeter: at most one fix batch runs per git org at a time, while unrelated git orgs dispatch fully in parallel.

Claiming a window is one atomic query against the git org's own row:

```sql
UPDATE organizations
SET last_dispatched_at = now()
WHERE id = :git_org_id
  AND (last_dispatched_at IS NULL
       OR last_dispatched_at < now() - make_interval(mins => :window_minutes))
RETURNING id
```

A returned row means this pod won the window for that git org. Nothing back means another pod already claimed it. The same query enforces both the timing rule and cross-pod race safety.

From there, the dispatcher grabs a capped batch with `FOR UPDATE SKIP LOCKED` and sends it to a single container. The cap keeps resource usage predictable: a container with 5 issues clones at most 5 repos (shallow, `--depth 1`), runs at most 5 parallel triage agents, and produces at most 5 fix branches. Anything past the cap stays eligible and rolls into the next window automatically.

One more thing is opt-in per Sentry org and off by default: a merge gate. Turn it on and a git org's next batch waits until every PR its last batch opened has merged or closed, so a forgotten open PR doesn't get buried under a second one on the same code.

## Two-phase container execution

The container runs in two phases, and the backend only ever waits on the first one.

### Phase 1: triage and synthesis (blocking)

Every issue in the batch is triaged concurrently. Each triage agent fetches the Sentry issue and its stack trace, clones the repo if the URL is known and reads the affected files for real context, classifies the issue as an actionable bug or infrastructure noise, checks the repo's existing PRs to avoid duplicating work or re-proposing something a human already closed, decides which repo (or repos) the fix belongs in, and outputs a structured result with confidence, affected files, root cause, and the findings the fixer will act on.

That last part matters: there's no separate planning agent behind it. Triage can't reach a verdict without reading the code, which is exactly what separates "the stack trace mentions this file" from "this is genuinely broken." Rather than throw that investigation away and pay a second agent to re-derive it from a summary, triage writes it down as findings and the fixer implements it directly.

Once every triage completes, a synthesis agent groups related issues by root cause: two null-pointer exceptions in the same service touching the same data layer become one group, an unrelated auth timeout gets its own. It returns only the grouping. The group's target repos, findings, and affected files are all rebuilt from the triage records it grouped, so a reworded or invented repo name in the synthesis output can't reach the code that opens PRs.

One PR opens per group, per target repo, on a single shared branch, minimal body, just the triage summary and the grouped issue links, no code yet. They open ready, never as a draft: a `Draft:` title makes GitLab skip the pipeline under a `workflow: rules` guard, and un-drafting isn't itself a pipeline trigger, so a draft MR would sit blocked on a pipeline that can never run.

The container reports issue-to-group mappings and PR URLs back. The next window's triage sees these open PRs and doesn't duplicate work on related errors that arrive later.

### Phase 2: fix (background)

Each group runs independently and concurrently. One fixer session spans every worktree in the group, working straight from triage's findings. A fix spanning a backend and its paired frontend is one session, one branch, and a PR in each repo, not two disconnected runs a reviewer has to correlate by hand.

Pushing isn't the end of the turn. The target repo's own CI has to pass on that commit before the fixer can finish, checked through GitHub's Checks and Status APIs or GitLab's pipeline status, whichever the provider exposes. A failure comes back to the agent with the failing step's logs so it can fix and retry, capped at six rounds. A repo with no CI, or a failure already red on the default branch, doesn't block, since there's no fixable signal either way. The runner re-checks CI on the final commit independently rather than trusting the agent's own account of what happened.

Every repo that received a real commit gets its PR labeled for review, green or not: the retry budget was the chance to fix CI, and a failure that survives it isn't grounds to leave working code unreviewed. A repo the fixer didn't end up needing gets its PR closed with a comment instead of left sitting empty.

Phase 2 is fire-and-forget from the backend's side. The container streams structured logs throughout, and a log watcher records the outcome. Failed groups re-enter the pool for the next window.

## What this looks like in practice

4 AM, a new deploy goes out, Sentry fires six errors. Two share a root cause (the same missing null check in two API handlers), two are infrastructure noise (a Redis timeout during the deploy), one is a genuinely new bug, and one already had a fix PR merged three weeks ago.

By the time the batching window closes: the two noise errors are triaged, marked not actionable, and cached. The two related errors become one group, one PR. The new bug gets its own group and PR. The one with a merged fix is skipped outright, since it already had its attempt.

Phase 2 runs in the background, and by the time engineers are online, those PRs already have real, CI-verified changes, and the review workflow has already been through them once.

## What's actually eligible, at any moment

There's no separate queue table behind any of this. Whether an issue is eligible for dispatch is computed at read time from the executions attached to it: no non-failed execution, under the retry cap, mapped repo enabled. Nothing is precomputed into a stored queue state.

That derivation is what makes the whole thing crash-safe. Retrying a failed issue is just its failed execution existing; there's no separate flag to flip back. And because eligibility is a query, not a cache, the entire state is inspectable from Postgres directly, no separate store to keep in sync with it.

## Self-hosting means your code stays yours

One constraint shapes everything above: the backend never sees source code. Triage, synthesis, and fixing all run inside tenant-scoped containers with direct access to the tenant's repo and Sentry token. The backend only ever stores issue IDs, statuses, PR URLs, and group relations.

Self-hosted, your proprietary code, your auth logic, your business rules never touch external infrastructure beyond your own LLM provider. The agent runs where your code already is, so the blast radius of a security incident stays bounded to your own infrastructure instead of someone else's.

---

The decision logic is specified in [ADR-001](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/001-issue-processing-decision-logic.md), the batching and partitioning model in [ADR-006](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/006-pending-pool-and-batch-dispatch.md), and CI-gated fix verification in [ADR-008](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/008-ci-gated-fix-verification.md). JeanClode is open source: the ingestion pipeline, dispatcher, and container lifecycle are all in the repo.
