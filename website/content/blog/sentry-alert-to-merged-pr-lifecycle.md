---
title: "From Sentry Alert to Merged PR: The Complete JeanClode Lifecycle"
description: "A complete walkthrough of what happens from the moment Sentry fires a webhook to a reviewed, mergeable PR: ingestion, the decision tree, batching by git org, the fix, CI verification, and the review loop."
publishedAt: "2025-06-24"
readingTime: 10
seoTitle: "Sentry Alert to Merged PR: The Full Lifecycle"
seoDescription: "The complete loop from Sentry webhook to a reviewed PR: ingestion, the decision tree, batching, triage, the fix, CI verification, and the review loop."
---

An autonomous coding agent is only as good as its full loop. A system that triages well but opens bad PRs is noise. A system that writes good fixes but can't tell when CI is broken is incomplete. JeanClode is built around a closed loop: a Sentry error goes in, a reviewed PR comes out.

This is a complete walkthrough of that loop, as it runs today.

## Step 1: Sentry fires a webhook

Sentry sends issue-level webhooks, not individual event webhooks. When a new issue appears, or an existing issue's status changes, Sentry fires a single webhook to JeanClode's ingestion endpoint.

The endpoint does the minimum: validate the HMAC signature, extract tenant ID, issue ID, and event metadata, push to a Redis list, return `200 OK`. No database calls, no business logic. Its only job is to acknowledge Sentry and not drop the event. If Postgres is slow or briefly unavailable, events buffer in Redis.

## Step 2: A consumer decides whether this issue is eligible

A pool of consumer workers pops events off the Redis queue and checks each issue against Postgres. An issue is dispatchable when its repo maps to an enabled git target, it has no fix execution beyond a `FAILED` one, and its failed-attempt count is under the retry cap.

In practice: never seen before, it becomes eligible. Already triaged as not actionable, it's skipped, cached, and stays skipped regardless of how many more times the same noisy error fires. Already running or already has an open or merged PR, skipped, since any of those counts as a non-failed execution on record. A human closed the PR, permanently skipped, and triage quotes their stated reason back rather than re-litigating it. The last attempt failed, it retries, up to a capped number of attempts.

This whole check runs in milliseconds against Postgres, no LLM calls yet, and there's no separate "pending" flag anywhere: an issue's eligibility is derived at read time from the executions linked to it, not written into a queue column a crash could leave stale.

Two things this step does not yet do, even though they're part of the target design: it doesn't escalate re-triage on a not-actionable issue no matter how much volume it gets, and it doesn't check a merged fix's release tag against an incoming regression. A `regression` webhook today just updates the issue's stored status. More on that at the end.

## Step 3: Batching by git org

Issues that pass Step 2 sit ready to be picked up. A dispatcher ticks every 30 seconds looking for `(sentry_org, git_org)` pairs eligible for a batch: at least one dispatchable issue, an expired window, and no fix batch already running for that git org.

```sql
UPDATE organizations
SET last_dispatched_at = now()
WHERE id = :git_org_id
  AND (last_dispatched_at IS NULL
       OR last_dispatched_at < now() - make_interval(mins => :window_minutes))
RETURNING id
```

A returned row means this pod won the window for that git org. Nothing back, another pod already claimed it. The window length is the Sentry org's own `batch_window` setting (5 minutes by default, configurable up to a week), and the git org, not the tenant, is what keeps two fix batches from stepping on each other: only one runs per git org at a time, while other git orgs dispatch fully in parallel.

This is why related errors end up grouped. A deploy that causes six errors at once doesn't spin up six containers. Within one window, all six land in the same batch and dispatch together.

One Sentry org's projects can map into more than one git org, in which case each git org gets its own independent partition, its own window, and (if enabled) its own merge gate.

## Step 4: container Phase 1: triage and synthesis

The container runs the JeanClode CLI with every batch issue and its resolved repo:

```bash
jeanclode issue1 issue2 issue3 --repo org/api issue4 --repo org/billing
```

Each issue is triaged concurrently. For each one: fetch the Sentry issue and stack trace, shallow-clone the repo if a URL is known, read the affected files to verify the bug against real source, classify it as an actionable bug or infrastructure noise, check the repo's existing PRs (an open one means work is already underway, a closed unmerged one means a human already declined this fix), decide which repo the fix belongs in, and output a structured result with confidence, affected files, root cause, and the findings the fixer will use.

Triage doubles as the planner here. It can't reach a verdict without reading the code, so it already holds the investigation a separate planning agent would otherwise have to redo from a cold context. Those findings go straight to the fixer.

A deterministic filter, no LLM involved, then drops issues that aren't actionable, already have a PR, were previously rejected, or whose target repo isn't actually checked out on disk. Repos are matched by name against what's really cloned, never by a URL a model supplied.

If two or more issues are actionable, a synthesis agent groups them by root cause: two null-pointer exceptions in the same service and data layer become one group, an unrelated auth timeout gets its own. It returns the grouping and nothing else. Every other field, target repos, findings, affected files, is rebuilt from the triage records it grouped, so a reworded or invented repo name can't reach the code that opens PRs against it.

One PR opens per group, per target repo, all on one shared branch, minimal body, no code yet. This is also a coordination signal: the next window's triage sees these PRs and won't duplicate work on related errors that arrive afterward. They open ready, never as a draft, since a `Draft:` title makes GitLab skip the pipeline under a `workflow: rules` guard, and un-drafting isn't itself a trigger, leaving a draft MR blocked on a pipeline that can never run.

The backend only waits on Phase 1. It's fast, typically a few minutes for parallel triage across a batch. The next dispatch window can open before Phase 2 even finishes.

## Step 5: container Phase 2: fix

Each group is processed concurrently. Every repo the group targets gets its own git worktree on the group's shared branch, and one fixer session runs across all of them, working from triage's findings plus each repo's path and PR URL.

One session, not one per repo: a fix spanning a backend and its paired frontend is one change, and splitting the reasoning across two independent sessions is how you get a PR pair that only makes sense in the author's head.

The fixer has the full repo and shell tools, running tests, reading files, grepping, the way a developer would rather than generating code in isolation.

If the fixer fails on one group but succeeds on another, the successful group's PR is still ready. The failed group re-enters the pool for the next window. Partial success is the default outcome, not an edge case.

## Step 5.5: CI-gated verification

A pushed commit isn't the end of the fixer's turn. The target repo's own CI has to pass on that commit first, checked through GitHub's Checks and Status APIs or GitLab's pipeline status. A failure comes back to the agent as new context, including the failing step's logs, and it retries: fix, push, check again, up to six rounds.

Two cases don't gate at all: a repo with no CI configured (nothing to wait on), and a failure already red on the target repo's own default branch (pre-existing breakage the fix didn't cause and can't fix). Both are checked before deciding whether a failure actually blocks anything.

What happens if a failure survives all six rounds is worth being explicit about: the PR still gets labeled for review. CI is a signal for the fixer to act on mid-session, not a verdict on whether the work deserves a human look, and a run only reports failure when the run itself broke (nothing pushed, or something threw). The CI outcome gets recorded per repo either way, so nobody has to guess. The runner independently re-checks CI on the final commit rather than trusting the agent's own retry loop.

## Step 6: the review loop

The PR gets a `jeanclode:review` label, and that label is itself a webhook trigger. So the next thing that reads the PR isn't a human, it's the review pipeline: seven agents, two of them parallel analyzers with code search tools, and a fact-checker that starts from fresh context and re-verifies every claim against the actual code.

If it finds nothing, it posts LGTM and the loop is done. If it finds something, it posts inline comments and one more: `@jeanclode-bot handle all the comments above, verify each isn't a false positive or already resolved first.` That mention dispatches the respond workflow, which works through the findings one at a time. A real one gets fixed and the thread resolved. A false positive gets the thread resolved with an explanation and no code change.

Then a deterministic check runs, deterministic because the agent's own account of what it did isn't evidence: the workflow compares the branch's tip on `origin` before and after the turn. If it moved, `jeanclode:review` is stripped and re-added, forcing a fresh webhook, and review runs again on the new commit.

That's the loop: review, sweep, push, re-review. It converges two ways, and both end with the people your org configured @-mentioned in a comment, once, guarded by a marker so whichever exit fires second doesn't post it twice:

- Review comes back clean.
- A sweep resolves the last open thread without pushing anything, because every remaining finding was a false positive, so there's no new commit to trigger a re-review and no LGTM ever coming.

The self-mention that starts a sweep only ever fires on a PR the bot itself opened, and the respond planner is explicitly forbidden from emitting it on its own. That's the whole loop guard: there's exactly one way in, and it isn't a model's decision.

**Then a human reviews it.** Correct, merge it. Wrong, close it. If you close it, JeanClode records the PR as rejected. Triage skips this issue from then on and quotes your stated reason rather than re-arguing it. We don't second-guess human reviewers.

## An opt-in gate between batches

One thing that isn't part of the numbered steps but does hold real weight in production: a git org can turn on a merge gate. With it on, that git org's next fix batch waits until every PR its last batch opened has been merged or closed, rather than opening a second batch of fixes on top of PRs still sitting open. It's off by default; when it's on, there's no age-based escape hatch, so a forgotten open bot PR holds that git org's Sentry fixes until someone deals with it.

## What comes after merge

Today, merging is where JeanClode's involvement with that issue ends. An issue with a non-failed fix execution never re-enters the pool, so a merged fix means nothing gets stacked on top of it. If the same error comes back, Sentry marks the stored issue unresolved again and it shows up in the dashboard, but nothing re-dispatches on its own.

Closing that loop fully means two more moves, specified but not yet built: resolving the issue in Sentry the moment its PR merges (which is what makes Sentry fire a `regression` event if the error reappears), and, when that regression event does fire, checking the fix's release tag against the error's to tell "hasn't deployed yet" apart from "shipped and didn't work" before deciding whether to re-process. Right now a `regression` webhook only updates the stored issue status. It doesn't yet trigger a second attempt.

## The full loop, as it runs today

```
Sentry webhook
  → Redis queue (ingestion, <1ms)
    → Consumer: eligibility check (Postgres, <10ms)
      → batch window per (sentry_org, git_org) pair
        → Phase 1: parallel triage + synthesis + PRs (a few minutes)
          → Phase 2: one fixer session per group (background, longer)
            → push → CI gate (retry on failure, 6 rounds) → label for review
              → Review loop: review → sweep findings → push → re-review
                → converged → @-mention the reviewers you configured
                  → Human merges
```

Each stage has one job, failures in one don't take down the others, and the loop is closed everywhere except the last mile back to Sentry.

## What this means for on-call

For teams self-hosting JeanClode, the steady state looks like this: errors arrive overnight, get batched by git org, and PRs are waiting before standup, already through several rounds of review, with the obvious findings fixed and the false positives argued down already. Engineers review instead of triage. The mechanical class of bugs, null checks, missing guards, off-by-one errors, gets handled without anyone paging on-call for it.

---

The full implementation is open source. The decision logic is in [ADR-001](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/001-issue-processing-decision-logic.md), the batching and partitioning model in [ADR-006](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/006-pending-pool-and-batch-dispatch.md), the ingestion pipeline in ADR-004, and CI-gated fix verification in ADR-008.
