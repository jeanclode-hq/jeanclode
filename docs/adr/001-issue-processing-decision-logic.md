# ADR-001: Issue Processing Decision Logic

## Status

Accepted — partially implemented.

The dispatch-eligibility half is live, enforced by `db_get_dispatchable_issues`
(see ADR-006): an issue with a non-`FAILED` fix execution is never dispatched
again, and failed ones retry up to the cap. Scenario 6 (a rejected PR is final)
is additionally enforced inside triage itself, which classifies a closed-unmerged
PR for the same root cause as `previously_rejected`.

Two branches described below are **not** implemented yet:

- **Scenario 5's escalating re-triage threshold** for high-volume noise. There is
  no volume-based re-triage; a not-actionable issue stays not-actionable.
- **Scenario 7's regression handling.** Jeanclode does not resolve the Sentry
  issue on merge, and does not compare a fix against the error's release tag. A
  `regression` webhook updates the stored issue status and nothing more —
  `backend/api/routers/webhooks/sentry/triage.py` carries the `TODO`, and its
  `decide()` function has no callers.

The decision tree below is the target design, not a description of current
behavior.

## Context

Jeanclode receives Sentry webhook events and must decide whether to process each issue (triage, fix, open PR) or skip it. Processing is expensive (LLM API calls, CLI agent time) so we must avoid redundant or wasteful runs.

We identified 7 core scenarios that the decision logic must handle correctly.

## Scenarios

### 1. New single error, real bug

First time Jeanclode sees this issue. No prior record.
**Outcome:** Process it. CLI triages, plans, fixes, opens PR.

### 2. Recurring error (pre-existing), real bug

Error has been in Sentry for months, but Jeanclode has never processed it.
**Outcome:** Process it. From Jeanclode's perspective, this is the same as scenario 1.

### 3. New single error, not actionable

Error is infrastructure noise (database connection timeout, transient network failure, etc.).
**Outcome:** CLI triages it as not actionable. Store the result. Skip all future events for this issue.

### 4. High-volume error, actionable

Same error fires 30K times in 10 days. Sentry groups all events under one issue ID.
**Outcome:** Process it once. The volume is irrelevant, it's one issue. Fix it, open PR.

### 5. High-volume error, not actionable

Same as above but the error is infrastructure noise.
**Outcome:** Triage once, mark as not actionable. Skip future events. However, if event volume crosses an escalating threshold, re-triage, the initial classification may have been wrong, or the error's nature may have changed. The threshold grows exponentially to prevent repeated re-triage of persistent noise.

### 6. Previously attempted, PR rejected

Jeanclode opened a PR but a human reviewer rejected/closed it.
**Outcome:** Don't retry. A human decided the fix was wrong. Jeanclode should not second-guess that.

### 7. Regression, PR merged, error returns

Jeanclode fixed the issue and the PR was merged, but the error reappears. When a PR is merged, Jeanclode auto-resolves the corresponding issue in Sentry via the API. This ensures Sentry fires a `regression` webhook if the error reappears, which is how the issue re-enters our pipeline.

**Outcome:** Depends on whether the fix has been deployed:

- **No release tag on the error**, Skip. We cannot confirm the fix is deployed, so we assume it hasn't reached production yet.
- **Fix is included in the error's release tag**, Process again. The fix didn't work. The codebase already contains the previous attempt, so the CLI agent will see it and produce a different fix.
- **Fix is NOT in the error's release tag**, Skip. The fix exists but hasn't been deployed to the release that's producing errors.

## Decision

### Decision tree

```md
issue arrives via webhook
  |
  +-> never seen before                → PROCESS
  |
  +-> last outcome: not actionable
  |     +-> volume below threshold     → SKIP
  |     +-> volume above threshold     → RE-PROCESS (threshold grows exponentially)
  |
  +-> last outcome: running            → SKIP
  |
  +-> last outcome: PR open            → SKIP
  |
  +-> last outcome: PR merged
  |     +-> no release tag on error    → SKIP
  |     +-> fix in release tag         → PROCESS (regression)
  |     +-> fix not in release tag     → SKIP (not deployed yet)
  |
  +-> last outcome: PR rejected        → SKIP
  |
  +-> last outcome: failed             → RETRY (exponential backoff, capped)
```

### Key principles

1. **One issue ID = one processing slot.** Sentry groups events into issues. We deduplicate on issue ID, not on individual events.
2. **Triage results are cached.** Once an issue is triaged as not actionable, we don't re-triage unless volume escalates.
3. **Human decisions are final.** A rejected PR means stop. We don't override human reviewers.
4. **Deployment awareness via release tags.** A merged PR doesn't mean deployed. We check the Sentry event's release tag to determine if the fix has reached the environment producing the error.
5. **Failures get retried with backoff.** Transient failures (API errors, CLI crashes) are retried with exponential backoff up to a maximum cap.
6. **Escalating re-triage threshold.** Not-actionable issues can be re-triaged if volume spikes, but the threshold grows exponentially to prevent thrashing.

## Consequences

- Issues triaged as not actionable are efficiently skipped on subsequent events.
- Rejected PRs are never retried, avoiding wasted compute and reviewer annoyance.
- Regressions are caught after deployment by checking release tags.
- The exponential threshold for re-triage balances false negatives against cost.
- **Scenario 8 (cascade, multiple related errors from the same root cause) is explicitly out of scope** and will be addressed in a separate ADR.
