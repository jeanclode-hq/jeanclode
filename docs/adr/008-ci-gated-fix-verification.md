# ADR-008: CI-Gated Fix Verification (ci-watch)

## Status

Accepted

## Context

The fixer agents (Sentry fix and issue-resolve pipelines, ADR-007) implement
a fix, run a static check only (syntax/type-check, no runtime tests), push,
and the runner unconditionally marks the PR ready and attaches the
`jeanclode:review`/`jeanclode:summary` labels. Nothing in the pipeline ever
verifies the fix actually builds or passes tests.

We considered several ways to let the fix agent verify its own changes before
finalizing a PR: a toolchain base image (+ `mise` for runtime version
pinning), lockfile-based dependency install, a localhost-only browser sandbox
for visual/e2e checks, translating a repo's `docker-compose.yml` into pod
sidecars, and environment snapshotting (PVC cache or CSI `VolumeSnapshot`) to
avoid cold installs every run.

**Decision: don't build any of that.** Instead, lean on the repo's own CI —
it's already the source of truth for "does this repo build and pass," it's
already trusted by the repo owner, and it runs on GitHub's/GitLab's
infrastructure instead of ours. This trades a tight in-sandbox edit-test loop
for a slower, async, but far simpler verification signal. That trade is
acceptable here: the pipeline is already async end-to-end (webhook →
5-minute batch window → background fix phase, ADR-002), nobody is watching a
spinner, and if something's urgent a human would just do it themselves
rather than wait on the bot.

**Explicitly out of scope / superseded by this direction:** polyglot
toolchain image, dependency auto-install, browser sandbox,
docker-compose-to-sidecar translation, dependency/environment snapshotting.
None of that gets built unless this approach turns out to be insufficient in
practice.

## Decision

### Flow

```
fixer agent: implement fix -> push commit -> call StructuredOutput to finalize
  -> PreToolUse(StructuredOutput) hook (require_ci_pass_hook) intercepts:
     marks the PR ready on first check (so CI starts), runs check_ci
       - "finish"  -> allow StructuredOutput through, turn ends
       - "failure" -> has the agent already dropped a bypass marker for
                      this repo (bypass_marker_path)? -> allow it through,
                      no further checks
                    - otherwise -> deny StructuredOutput, feed the result
                      back as tool output as if the agent were told "not
                      done yet, here's why" -> agent either fixes and
                      pushes again, or (if it's confident the failure isn't
                      its own) writes the bypass marker -> tries to
                      finalize again -> loop, until "finish", a bypass
                      marker appears, or an in-session retry budget is
                      exhausted (then the deny stops firing regardless)
  -> runner independently re-checks CI on the final commit (same trust
     boundary as verify_fix_pushed — never infer "CI passed" from the hook
     having let the agent finish), for the record — then attaches
     `jeanclode:review`/`jeanclode:summary` and reports the run itself as
     "success" unconditionally. CI's outcome never gates either one.
```

Why `PreToolUse(StructuredOutput)` and not `Stop`: the fixer agents run
with `output_schema` set, so they end a turn *by* calling the SDK's
`StructuredOutput` tool. A `Stop` hook fires *after* that call has already
finalized the turn — and the CLI then drops the hook's block rather than
retract the submitted structured output
(`tengu_structured_output_late_retraction_drop`), so the fix-and-recheck
loop never actually looped: `check_ci` ran once, the block was discarded,
and the fixer exited without ever seeing the CI failure (observed in
production on a red MR). `PreToolUse` fires *before* `StructuredOutput`
runs, so `permissionDecision: "deny"` reaches the agent as tool output and
it keeps working in the same session — cache and context intact — until CI
is green, bypassed, or the budget is spent. `require_pushed_fix_hook` stays
a `Stop` hook (it has no such interaction and also needs to fire when the
agent hits `max_turns` without ever calling `StructuredOutput`); the
runner's own post-invoke `check_ci` + `verify_fix_pushed` are the
authoritative backstop either way.

The bypass marker (`bypass_marker_path(cwd)`, `src/agents/hooks.py`) is a
plain file the fixer creates via Bash (`touch`) — a tool it already has, so
this adds no new tool surface — one directory above its worktree (never
inside the git tree, so a broad `git add` can't sweep it up). `fixer.md`
tells the agent exactly where to touch it per repo. Existence is the whole
signal — the agent isn't asked to explain itself, since nothing downstream
ever reads the file's content; the hook logs a fixed, generic message when
it sees the marker, which is all a human reviewing the run needs. Its only
effect is cutting the CI gate hook's retry loop short *for that one repo*; it
never affects whether the PR gets labeled — that was already unconditional
(see below) before this existed. The point is purely to stop making the
agent sit through retries it already knows won't help: without it, a
failure the agent correctly judges as unrelated still burns the full retry
budget before the hook lets go on its own, each round costing a real (if
usually fast) turn for no benefit.

CI is a mid-session signal the fixer acts on, not a gate on the PR itself:
it drives the fix-and-recheck loop while the agent is still working, but
once that loop ends — green, still red, or no CI configured at all — the
PR gets labeled either way, full stop. Earlier versions of this pipeline
tied labeling itself to a still-red final check being a hard failure
(optionally overridable by the fixer's own "this isn't my fault" verdict),
but that meant a failure that outlived the retry budget — for any reason,
ours or not — silently produced neither a green PR nor a labeled one: the
run just ended with nothing to show for it. Since the in-session loop
already gives the fixer real chances to fix anything it broke, a failure
that survives it is better surfaced as a labeled PR a human can look at
than as a run that quietly did nothing.

`WorkflowResult`/`GroupResult` status follows the same rule as labeling: a
still-red CI check is not a run failure. `"error"` is reserved for the run
itself breaking — the fixer never pushed anything real, or the pipeline
hit an exception — the same meaning it already had before this ADR. A
CI failure is a property of the *generated fix*, not of whether jeanclode's
own pipeline did its job; conflating the two would mean a perfectly
functional run (triage, fix, push, PR, labels — all worked) reports as a
pipeline failure just because the repo's tests didn't like the fix. The
`ci` field on the result (`repo_results[name]["ci"]` / `GroupResult.ci`,
see "Implementation notes") is how that information actually reaches a
human — as data on a successful run, not as the run's own status.

This is a hook, not a tool the agent calls voluntarily. A tool call is
something a model can skip, forget, or judge unnecessary — the whole
verification would be bypassable by an agent that just doesn't call it. A
`PreToolUse(StructuredOutput)` hook fires on *every* attempt to finalize
the turn and has no opt-out; the agent doesn't need to know it exists for
it to apply. Same rationale as the existing `require_pushed_fix_hook`
(`src/agents/hooks.py`), which this sits alongside (on `Stop`, for the
reasons in "Flow" above).

### Gating table

`check_ci`'s outcome only ever drives the in-session retry loop — it never
decides whether the PR gets labeled, that's unconditional once a real fix
is pushed:

- No CI configured, or CI blocked from running (`action_required`, `stale`,
  `skipped`) → finish, nothing to retry on.
- Running → wait (bounded); success → finish; failure → a bypass marker for
  this repo already present → finish, no further checks; otherwise → feed
  the result back to the agent to fix again, up to the in-session retry
  budget; wait itself timing out → treated as a failure, same as a real
  one, for that same retry accounting.

### Implementation notes

- **One deterministic function** (`check_ci`, `cli/src/activities/ci_watch/`)
  backs both call sites — the `PreToolUse(StructuredOutput)` hook and the
  runner's own final check — so "the hook said CI passed" and "the runner's
  own check says CI passed" can never silently diverge.
- **Off the event loop**: `check_ci` is synchronous (subprocess calls, plain
  `time.sleep` waits) and both call sites are inside `asyncio.gather`'d
  concurrent work (multiple groups' fixer sessions, ADR-002's batch
  processing) — both the hook and the runner's final check invoke it via
  `asyncio.to_thread` so one group's multi-minute CI wait doesn't stall
  every other group's progress.
- **GitHub**: both signal APIs are queried (Checks API for Actions/most
  integrations, legacy Status API for third-party CI) since either can carry
  the only signal for a given repo. Required-status-checks are read
  best-effort to weight which failures actually block gating; unreadable
  (403/404) falls back to treating every check as required. Failing-step logs
  use `gh run view <run-id> --log-failed`, which handles the zip/redirect
  complexity of the raw Actions logs API internally.
- **GitLab**: the MR's pipeline `status` is read as authoritative rather than
  fanning out per-job — `manual` jobs never block, `allow_failure` jobs don't
  fail the pipeline. Job traces (`.../jobs/:id/trace`) are plain text
  directly, no redirect/zip involved.
- **Attribution is agent-judged, not baseline-diffed, and only ever informs
  the in-session retry loop — never a final gate.** `check_ci` only answers
  "is CI currently green on this commit" — it does not try to decide *why*
  a failure happened. An earlier version compared a failing required check
  against the default branch's latest pipeline result and excluded it from
  gating when that was also red, on the theory that "pre-existing" was
  mechanically detectable. It wasn't: on a repo whose default branch hadn't
  been pushed to in weeks, that "latest result" was three and a half weeks
  stale, so a CI-secret/runner failure that had nothing to do with the diff
  (an SSH deploy key rejected by the runner's libcrypto — every job died in
  `before_script`, before any actual check ran) read as "not on the
  baseline" and gated anyway, with a fixer agent that had no way to unblock
  a problem that wasn't in the code. A later version moved attribution to
  the fixer's own judgment instead — a structured `ci_bypass` verdict it set
  at the end of its run, read by the runner's final gate. That fixed the
  mechanical false-positive but introduced a different failure: an
  unrecorded verdict (retry budget exhausted mid-fix, parse failure, an
  agent that never got asked) defaulted to "not bypassed," so a failure with
  nothing to do with the fix could still end the run with no PR labeled at
  all — silently, since nothing distinguishes "the agent correctly judged
  this as its own fault and couldn't fix it in time" from "the verdict was
  never recorded." The current version (bypass marker file, see "Flow"
  above) keeps the same judgment call — fix it if the log points at your
  change, don't chase it if it doesn't — but the stakes of getting it wrong
  are now asymmetric: writing the marker only skips retries the agent
  believes are pointless, it can never skip the label itself, and not
  writing it just means the retry budget runs its course before the PR
  gets labeled anyway. There's no failure mode left where the judgment
  (or the lack of one) costs the run its PR.
- **Answer-only issue-resolve runs are outside the gate.** When triage
  sets `code_change=false` (the issue asks for information, not code), the
  runner creates no worktree or branch and attaches neither the push hook
  nor the CI hook, so there is no commit, PR or CI to verify. Triage makes
  that call, never the fixer: a fixer allowed to skip the push itself could
  answer its way out of a fix it's stuck on, which is exactly what
  `require_pushed_fix_hook` exists to stop.
- **Pagination**: every list-returning call requests `per_page=100` (both
  providers' max). Verified against a live 25-job GitLab pipeline where the
  unpaginated default (`per_page=20`) silently dropped the one
  non-`allow_failure` failing job — the gating verdict stayed correct
  (pipeline `status` is a single field, not paginated) but the log excerpt
  fed back to the agent would have been empty.
- **No PR is ever opened as a draft** — sentry-fix opens its placeholder PR
  before the fixer runs (see ADR-007), issue-resolve opens one lazily the
  first time it sees a real pushed commit (`require_pushed_and_ci_pass_hook`
  in `src/agents/hooks.py`, a sibling of `require_ci_pass_hook` that opens
  the PR itself via callback, per repo, instead of requiring one to already
  exist). Both open it ready.
  - Draft state used to be sentry-fix's default, with the CI gate hook
    flipping it to ready on the first real commit. That deadlocked under a
    `workflow: rules` guard of the shape
    `$CI_MERGE_REQUEST_TITLE =~ /^Draft:/ → when: never`: the fixer's push
    fires a `merge_request_event` while the title still says `Draft:`, so no
    pipeline is created, and un-drafting afterwards is not a pipeline trigger
    in GitLab. The MR was left on `ci_must_pass` with `head_pipeline: null`
    forever, and `check_ci` couldn't tell "no pipeline exists" from "CI is
    fine". Opening ready means the MR-creation event itself is the trigger,
    on a clean title.
  - `check_ci` (this ADR's actual subject) is identical either way; only
    when the PR comes to exist differs between the two workflows.
- **In-session retry budget**: reuses the same `_MAX_BLOCKS = 6` cap
  `require_pushed_fix_hook` already uses — after 6 denied `StructuredOutput`
  attempts the hook stops denying (lets the turn finalize) rather than
  looping forever; `max_turns` is the ultimate backstop either way. The
  bypass marker doesn't change this cap, it just gives the agent a way to
  exit the loop before hitting it.
- **Bypass marker, not structured output.** The obvious alternative —
  reusing `output_schema` the way the superseded `ci_bypass` verdict did —
  doesn't work here: the agent's own `output_schema` result is only
  readable once the whole session ends, but the hook has to decide
  *mid-session*, on every finalize attempt, whether to keep denying. A
  plain file the agent creates with Bash (a tool it already has — no new
  tool surface) is the simplest thing the hook can check synchronously on
  each attempt. `bypass_marker_path(cwd)`
  computes the same path independently in both the hook and the prompt
  (one directory above the worktree, named after it) — deterministic from
  `cwd` alone, so no extra plumbing between them.
- **K8s Job timeout**: bumped from 600s to 3600s (`backend/configs/kubernetes.yaml`,
  mirrored in `docker.yaml`) to accommodate several push→check_ci rounds
  within one fixer session, each with its own bounded wait.

### Required permissions

No new webhooks — `ci-watch` polls the provider APIs directly rather than
consuming `check_run`/`check_suite` events.

- **GitHub**: needs four scopes beyond what PR creation alone requires —
  **Checks: Read**, **Commit statuses: Read**, **Actions: Read** (for
  `gh run view --log-failed`), and **Administration: Read** (for
  branch-protection/required-status-checks — the broadest of the four).
  Declared in the App manifest generated by
  `backend/api/routers/admin/route.py:admin_github_manifest`
  (`default_permissions`: `checks`, `statuses`, `actions`, `administration`).
- **GitLab**: no new scope. The pipeline/job/trace endpoints are read-only
  and already covered by the `api`-scoped Group Access Token tenants provide
  today (ADR-003) — that scope already had to cover push + MR creation.

## Consequences

- Fixes are verified against the same signal the repo's maintainers already
  trust, with no new sandboxed test infrastructure.
- Verification is inherently async and can add several minutes per retry
  round to a fix's time-to-PR; acceptable given the pipeline is already
  async end-to-end.
- A repo with flaky or slow CI can still burn the in-session retry budget
  without a code-level cause, if the agent isn't confident enough to write
  the bypass marker — the PR gets labeled and the run still reports
  success either way, it just doesn't get the benefit of any retries that
  would've helped.
- Required-status-checks weighting and GitHub Actions log retrieval both
  depend on `gh`/`glab` CLI behavior rather than raw REST calls, which
  sidesteps most of the complexity (log redirect/zip handling, self-hosted
  host resolution) that a from-scratch REST client would need — but ties the
  implementation to CLI availability/behavior in the sandboxed container.
- **A red PR gets labeled for review, and the run reports success — CI
  status doesn't reach the dashboard as a pipeline failure.** A genuinely
  broken fix — one the in-session retry loop didn't manage to resolve, and
  that the fixer never marked as unrelated — still reaches a human as a
  labeled PR rather than as a silently abandoned run, same as before, but
  there's no separate "needs attention" run status distinguishing it from
  a clean green fix; the `ci` field on the result
  (`repo_results[name]["ci"]`, `GroupResult.ci`) is the only place that
  distinction lives, and reading it isn't part of anyone's workflow today.
  This is accepted because the alternative — treating a still-red PR as a
  pipeline failure — was tried and rejected: it made `WorkflowResult`
  status track something outside the pipeline's own control (a repo's
  tests, flaky CI, an unrelated infra blip), which is a worse signal for
  "did jeanclode's own run work" than for "should someone look at this PR
  before merging," and the label already answers the second question. If
  silent, review-worthy CI failures turn out to get missed in practice, the
  next step is probably surfacing `ci: "failure"` more visibly (a distinct
  label, a dashboard filter) rather than folding it back into run status.
- **Open question for later**: GitLab has no clean per-MR equivalent to
  GitHub's required-status-checks endpoint for weighting which pipelines/jobs
  are actually merge-blocking versus advisory beyond the pipeline's own
  overall `status` — if that turns out to be too coarse in practice, this
  needs a closer look.
