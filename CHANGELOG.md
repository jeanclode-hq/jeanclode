# Changelog

## 0.8.3 (2026-09-13)


### Performance Improvements

* **backend,frontend:** cache workspace stats and load active executions in one query
* **backend,frontend:** coalesce and jitter SSE refetches, one event per change

## 0.8.2 (2026-09-13)


### Bug Fixes

* **backend:** cut auth to one Redis call per request and queue on pool exhaustion

## 0.8.1 (2026-09-12)


### Bug Fixes

* **backend:** stop 500ing GET /workspaces/{id}/executions on stale current_step

## 0.8.0 (2026-09-12)


### Features

* **backend,frontend:** show active executions on the dashboard instead of leaderboards


### Bug Fixes

* **frontend:** draw cancel accent's wavy edge with an SVG path
* **frontend:** fix cancel button edge centering and cursor
* **frontend:** flush cancel accent to the pill edge, symmetric wave
* **frontend:** narrow cancel pill, calm its animation, harden hover expand
* **frontend:** rework cancel affordance as a single bouncing/pinging pill
* **frontend:** switch cancel button edge animation from wave to bounce


### Performance Improvements

* **backend:** parallelize k8s API calls in start_container


### Reverts

* **frontend:** go back to the bouncing/pinging pill over the SVG wave

## 0.7.0 (2026-09-11)


### Features

* **backend,frontend:** cancel-from-row-button for issues and PR reviews
* **backend,frontend:** support cancelling review executions from the PR page


### Bug Fixes

* **backend,frontend:** restrict cancel to RUNNING, not QUEUED
* **backend:** queue respond dispatches behind an in-flight one, fix cross-workflow gating

## 0.6.1 (2026-09-11)


### Bug Fixes

* **backend:** satisfy mypy and regenerate API types for cancel endpoint

## 0.6.0 (2026-09-11)


### Features

* **backend,frontend:** support cancelling running executions

## 0.5.0 (2026-09-11)


### Features

* **git-integration:** add who-can-trigger setting for [@jeanclode-bot](https://github.com/jeanclode-bot) mentions


### Bug Fixes

* **backend:** match issues date-range filter to the Date column shown

## 0.4.1 (2026-09-11)


### Bug Fixes

* **backend:** scope a fix PR to the issue(s) it actually addresses
* **cli:** lead branch names with the number, add one for sentry-fix

## 0.4.0 (2026-09-11)


### Features

* **frontend:** show linked pull requests on the issue detail modal

## 0.3.1 (2026-09-11)


### Bug Fixes

* **frontend:** expose Sentry backfill scope in onboarding connectors step

## 0.3.0 (2026-09-10)


### Features

* per-git-org Sentry batching, execution↔PR links, and a merge gate


### Bug Fixes

* **backend:** scope organization access to the workspace, not provider membership
* **cli:** declare httpx as a runtime dependency
* **cli:** ship agent prompts in the wheel and stop faking LGTM

## 0.2.1 (2026-09-08)


### Bug Fixes

* **backend:** give container runs a full hour before the deadline

## 0.2.0 (2026-09-08)


### Features

* pin repos and pack subgroups into multi-repo runs
* **website:** canonical URLs, full og/twitter tags, and schema markup
* **website:** ink-drawing landing and pricing, WebGL renderer, self-hosted fonts
* **website:** rework the landing page and restore its theme colors


### Bug Fixes

* **website:** stop pinning the landing story on phones and tablets
* **website:** stop the landing story juddering on mobile scroll

## 0.1.1 (2026-09-07)


### Bug Fixes

* **cli:** add README so the PyPI project has a description

## 0.1.0 (2026-09-07)

First public release of Jeanclode — a self-hosted autonomous coding agent that
listens to Sentry errors, GitHub, and GitLab events and runs purpose-built
multi-agent workflows on the Claude Agent SDK to triage and fix issues, opening
pull requests without human intervention.

### Workflows

* **sentry_fix** — multi-phase pipeline: fetch → parallel triage (picks the
  target repo(s) and writes the fix plan) → synthesis → fix → CI-gate and label.
  A single root cause becomes one MR, and a fix can span several repos, each with
  its own worktree and PR on one shared branch.
* **code_review** — 7-agent pipeline (IssueExplorer → 2× Analyzer → Synthesizer →
  Deduplicator → FactChecker → Guardrail → Styler) posting inline comments on
  GitHub PRs and GitLab MRs, with a follow-up sweep / ready-notice loop on
  bot-opened PRs.
* **pr_summary** — writes the PR/MR description, carrying over any Sentry issue,
  related MR, or ticket link the previous description held.
* **issue_resolve** — triage → fix → open PR(s) for GitHub issues and GitLab
  issues/work items, lazily opening a PR per linked repo only once the fixer
  pushes to it.
* **jeanclode_respond** — handles `@jeanclode-bot` mentions, either routing to an
  existing pipeline or handling the ask directly, with deterministic post-turn
  checks against provider state.

### Platform

* FastAPI backend for webhook ingestion, queue dispatch, tenant management, and
  real-time SSE.
* Security proxy sidecar that injects git and LLM credentials into agent traffic
  without exposing them to the agent subprocess.
* Nuxt 4 dashboard — workspace, integrations, live execution feed, leaderboards.
* Repo groups for polyrepo / microservice setups, so one fix can coordinate
  changes across linked repositories.
* CI-gated fix verification: a fix is labeled ready only after the target
  repository's pipeline passes.
* Agent memory system for carrying learnings across runs.
* LLM credential failover across multiple keys with rate-limit handling.
* Configurable batch window between Sentry dispatch batches.

### Release automation

* Release Please cuts tags, GitHub Releases, and this changelog from conventional
  commits.
* CLI (`jeanclode`) published to PyPI via OIDC trusted publishing.
* Container images published to `ghcr.io/jeanclode-hq/{backend,frontend,cli,security-proxy}`.
