# ADR-002: Cascade and Batch Processing

## Status

Accepted

## Context

ADR-001 covers the decision logic for individual issues. This ADR addresses **scenario 8: multiple related errors arriving in a short time window** (e.g., after a bad deploy), where we want one PR per root cause, not one PR per error.

### Constraints

- **Multi-tenant cloud deployment.** The backend must never see code-level details (file paths, function names, source code). All code intelligence stays inside tenant-scoped containers.
- **Isolated containers.** Each tenant's work runs in an ephemeral container with access to the tenant's repo and Sentry token. Containers are destroyed after use.
- **Containers can't communicate.** No shared state between containers. The backend is the only coordination point, but it only knows issue IDs, statuses, PR URLs/numbers, and the execution↔PR links, not code.

### Scenarios

#### 8a. Multiple related errors arrive together

4 errors in a 5 min window. All caused by the same root cause.
**Outcome:** One PR.

#### 8b. Multiple errors, partially related

4 errors in a 5 min window. 2 share a root cause, 2 are separate.
**Outcome:** 3 PRs (one for the pair, one each for the others).

#### 8c. Multiple unrelated errors arrive together

4 errors in a 5 min window. All independent.
**Outcome:** 4 PRs.

#### 8d. Staggered related errors across windows

Error A in window 1. Error B in window 2, related to A.
**Outcome:** One PR. Window 2's triage sees window 1's open PR and evicts B.

## Decision

### Windowed batch processing

The backend collects incoming issues per **partition** on a rolling window. A partition is a `(Sentry org, git org)` pair — one Sentry org's projects can map into several git orgs, and a container carries exactly one git-platform token, so each pair batches independently (ADR-006). The window length is the Sentry org's `batch_window` setting: `5m` (default), `1h`, `12h`, `1d`, `3d` or `1w`. When a partition's window has elapsed and it has pending issues, they are dispatched to a single container as a batch.

Issues that have already been filtered by ADR-001's decision tree (skipped, already running, PR open, etc.) are **not included** in the batch.

### Batch size cap

Each batch is capped by the Sentry org's `batch_size` setting — `1`, `3`, `5` (default) or `10` issues. If more issues are pending than the cap allows, only the first N (oldest first) are dispatched. The rest remain pending and are picked up on a later window.

This bounds container resource usage:

- **Predictable cloning**, At most N repos cloned per container (worst case: all issues in different repos). Shallow clones (`--depth 1`) keep disk usage minimal.
- **Predictable compute**, Triage and fix time scales linearly with batch size. A small cap keeps containers fast and lightweight.
- **No overflow penalty**, Overflowed issues are still pending. They enter the next window's batch naturally, where ADR-001 filtering and cross-window deduplication (via the open PRs) apply as usual.

The cap is the primary scaling lever for container resources. Tuning it balances latency (smaller cap = faster containers, more windows to clear a backlog) against throughput (larger cap = fewer containers, more work per window).

### One batch per git org at a time

The git org (a GitHub org / installation, or a GitLab group) is the concurrency perimeter: only one fix batch runs per git org at a time. A fixer container clones repos, runs the multi-agent pipeline and drives CI; a second concurrent fixer on the same git org doubles that org's LLM (ADR-010) and container load and races the first on the same repos. The dispatcher starts nothing new for a git org while it has a `fix` execution `QUEUED` or `RUNNING`. Different git orgs dispatch in parallel (ADR-006).

### Two-phase container lifecycle

The container runs in two phases:

#### Phase 1: Triage + Synthesis

1. **Parallel triage**, One container runs a triage agent per issue concurrently. Triage is also the planner: it reads the source to reach its verdict and hands the fixer both its findings and the repo(s) the fix belongs in (ADR-007).
2. **Synthesis**, After all triages complete, a synthesis step groups related issues by root cause.
3. **PRs**, One PR is opened per group per target repo, carrying the triage summary. It is opened ready, never as a draft (ADR-007). Non-actionable issues produce no PR.
4. **Report back**, The container streams structured results (issue → group mapping, PR URLs and numbers). The backend updates its state as they arrive.

#### Phase 2: Fix

1. For each group, one fixer session implements the change across every target repo, working straight from triage's findings.
2. Push, then let the repo's own CI gate the turn (ADR-008). A repo the fixer turns out not to need has its PR closed rather than left empty.
3. Output logs for the backend to capture outcomes (PR labeled for review, failed, closed). On the terminal status the backend persists each PR and links it to the execution (ADR-006 / Part D).

Both phases run in the one container, so the `fix` execution stays `QUEUED` / `RUNNING` until phase 2 finishes — and the git org's next batch is held for that whole time. Letting a git org's fixer finish before its next batch starts is deliberate: it is what keeps that org's container and LLM load bounded.

### Cross-window deduplication

Because phase 1 opens the PRs, the next window's triage naturally deduplicates:

- Triage agents see the existing open PRs in the repo.
- If a new issue is related to an in-progress PR, triage evicts it from the batch.
- The backend also pre-filters using ADR-001 (issues with status "running" or "PR open" are skipped before reaching the container).

This handles scenario 8d (staggered arrivals) without any special logic. A tenant that wants a harder guarantee can enable the **merge gate** (`gate_on_open_fix_prs`, ADR-006): a partition then holds its next batch until every PR the previous batch opened has been merged or closed.

### Container overlap

Within a git org, consecutive windows are fully serialized — window N+1's container starts only after window N's has exited (one batch per git org), so its triage already sees every PR window N opened and phase-2 writes can't race. Across *different* git orgs containers can run at once, and safely: phase 1 (triage) is read-only, and phase 2 writes to git-org-scoped repos on per-group branches, so there are no shared resources to conflict on.

### Failure handling

- If phase 1 fails (container crash during triage), all issues in the batch return to the queue for the next window. Standard retry with exponential backoff per ADR-001.
- If phase 2 fails (crash during fix), the leftover PRs are closed and their bodies scrubbed of the Sentry issue links that would otherwise read as "already handled" to the next triage (ADR-010). The underlying issues re-enter the queue.
- Retry cap of 3 attempts per issue. After that, the issue is marked as permanently failed.

### Pipeline summary

```md
per (Sentry org, git org) partition, once its batch_window has elapsed
AND that git org has no fix batch currently running:
  |
  +-> backend collects pending issues (filtered by ADR-001, capped at batch_size)
  |   (overflow stays pending for a later window)
  |
  +-> dispatch ONE container with the batch
  |
  +-> phase 1:
  |     +-> parallel triage via sub-agents
  |     +-> synthesis: group related issues
  |     +-> open PRs (one per group, per target repo)
  |     +-> stream results: backend updates state
  |
  +-> phase 2 (same container):
  |     +-> for each group: one fixer session across its repos
  |     +-> push, CI gate, label for review, or close if not viable
  |     +-> stream outcomes via logs
  |
  +-> container exits -> execution terminal -> PRs persisted + linked
      -> this git org's next batch can now be dispatched
```

## Consequences

- **One PR per root cause**, even when multiple errors share the same underlying bug.
- **No code on the backend.** All intelligence (triage, grouping, fixing) stays in tenant-scoped containers. The backend only stores issue IDs, statuses, PR URLs/numbers, and issue-relation links.
- **The fix gates a git org's cadence.** A git org's next batch waits for its current fixer to finish — deliberately, so that org's container and LLM load stay bounded. Other git orgs keep moving.
- **Natural deduplication.** The open PRs serve as visible markers that prevent duplicate work across windows; the opt-in merge gate makes it a hard guarantee.
- **No overlap within a git org.** Consecutive windows for one git org are fully serialized, so its phase-2 writes can't race. Different git orgs run in parallel, on disjoint repos.
- **Retry is bounded.** Failed issues re-enter the pool, capped at 3 attempts.
- **Bounded container resources.** Batch size cap plus one-batch-per-git-org keep per-org memory, disk, and compute predictable. Overflow issues cascade into subsequent windows.
- **Open question for later:** Cluster separation, scaling beyond a single-pod deployment. The current design assumes one backend deployment coordinating all tenants' windows.
