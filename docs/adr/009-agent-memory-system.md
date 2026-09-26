# ADR-009: Agent Memory System

## Status

Accepted

## Context

Every workflow run starts from zero. An agent that triaged a Sentry error as a known false positive last week re-discovers that from scratch this week. A review agent corrected by a human on a repo-specific convention has no way to carry that correction into the next review of that repo. This is pure waste: the same investigation, repeated indefinitely, the kind of institutional knowledge a human reviewer accumulates just from working on a codebase over time.

Anthropic ships a built-in `memory_20250818` tool for exactly this shape of problem, but it doesn't fit here as-is, for two reasons:

1. **No backend.** The tool is client-side: the caller supplies the storage. Anthropic's reference implementations are single-user (a local filesystem directory). This system is multi-tenant: many workspaces, many concurrent sandboxed containers, no shared filesystem between them.
2. **Wrong framing.** The tool's auto-injected system prompt frames memory as session-recovery for one long task ("ASSUME INTERRUPTION: your context window might be reset at any moment... record status/progress in memory"). That's the opposite of what's needed here: durable knowledge shared across separate runs and containers, not a scratch pad for surviving a context reset within one run.

The `claude_agent_sdk` (the Claude Agent SDK used by the CLI) has no built-in memory tool at all, true of both the pinned and latest versions. What it does provide is `create_sdk_mcp_server` + `@tool` for defining custom in-process tools. The memory system is built on that: a backend store, a credential path to reach it from a sandboxed container, a CLI-side tool implementing it, and prompt framing that overrides the built-in tool's default assumptions.

## Decision

The system has four parts:

### 1. Backend: workspace-scoped storage and CRUD API

A `memory_entries` table (`workspace_id`, `path`, `content`, plus metadata), scoped by **workspace** rather than by repo. One workspace can span multiple repos, and a repo is just a path prefix inside the store (`/backend-repo/notes.md`). `(workspace_id, path)` is a database unique constraint, not an application-level check-then-insert, so two concurrent `create` calls at the same path resolve at the database level: one succeeds, one returns a documented error, and duplicates aren't possible.

A `/internal/memory` API implements the six commands from Anthropic's `memory_20250818` contract (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) as workspace-scoped storage with no line numbering, formatting, or truncation. That presentation layer belongs to the CLI-side tool, not the backend. This is the first container-initiated call path into the backend; every other integration in the system is dispatch-time push (backend to container) or passive log watching.

Two correctness requirements shape the write path:
- **Lost updates.** `str_replace`/`insert` do a read-modify-write, so without locking, two concurrent edits to the same file could silently overwrite each other. `SELECT ... FOR UPDATE` on the read prevents this: a second concurrent writer blocks until the first commits, then re-reads the already-updated content instead of clobbering it.
- **File/directory collisions.** A file and another entry's path prefix could otherwise share a name (`notes` as a file, `notes/x.md` as another entry); `view` on the file would then permanently hide the shadowed entries. `create`/`rename` reject both directions of the collision: the new path can't already be used as a prefix, and no ancestor segment can already be a file.

### 2. Backend: credential wiring

Sandboxed containers reach `/internal/memory` through the security-proxy sidecar, the same path they already use for Sentry, GitHub, and GitLab: never a direct connection, never a credential the agent process can see or construct itself.

Each execution gets a short-lived, HMAC-signed token (`workspace_id`, `execution_id`, expiry; 12-hour TTL, matching the security proxy's own per-execution CA cert lifetime), minted at dispatch time and injected by the sidecar as the `Authorization` header. Verification is self-contained (signature plus embedded expiry, no database round trip), keeping every memory call as fast and stateless as every other credentialed call in the system. `workspace_id` is a claim on the token, never a client-supplied field: a compromised or prompt-injected agent can't reach a different workspace's memory by changing a request parameter, because no such parameter exists.

The signing secret is never operator-configured. `get_or_create_memory_signing_secret` (`api.services.instance_settings`) checks the `instance_settings` table for an existing one and, if absent, generates a random 256-bit value and persists it encrypted, the same generate-once-and-share pattern the row's `(category, key)` unique constraint already gives every other instance-wide setting. A duplicate-insert race between two replicas booting simultaneously resolves the same way `memory_entries`' own create race does: the loser catches the `IntegrityError` and re-reads the winner's row. `Application.startup()` also calls this eagerly (best-effort, never fatal), so the secret exists from the first dispatch rather than being minted lazily on the first request. Every replica converges on the same value with no cross-pod coordination beyond the database they already share.

Wiring is unconditional: every org with a workspace gets memory credentials on every dispatch, with no per-workspace flag and no way to turn it off.

### 3. CLI: the tool itself

`claude_agent_sdk`'s `create_sdk_mcp_server` + `@tool` build an in-process MCP server exposing the same six commands, proxying to the backend endpoint. All presentation logic lives here, matching Anthropic's reference tool's documented behavior so a model's trained expectations of "the memory tool" aren't violated: 6-character right-aligned line numbers, `view_range` slicing, human-readable directory sizes, truncation past 16,000 characters, and the exact `"File {path} exceeds maximum line limit of 999,999 lines."` message past 999,999 lines. The agent's own HTTP calls never construct an `Authorization` header; that's the sidecar's job, transparently, the same as `gh`/`glab`.

### 4. CLI: wiring into agents

A `use_memory` flag on `BaseAgent`, mirroring the existing `use_third_party_skills` opt-in pattern: when set (and the run's workspace has memory enabled), the agent gets the six tools authorized, the MCP server registered, and a protocol block appended to its prompt.

That protocol block is where this system departs from Anthropic's reference framing. The built-in tool's auto-injected prompt reads "ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE... As you make progress, record status/progress in your memory... ASSUME INTERRUPTION," session-recovery framing for one task. This system's block keeps only the leading, emphatic "view first" instruction (proven-effective phrasing worth reusing) and replaces the rest: memory here is durable, cross-run, cross-workspace knowledge, explicitly not a place to log progress on the current run.

Enabled on judgment-heavy agents only, not every agent in every pipeline: Sentry-fix's Triage/Fixer (not Synthesis), issue-resolve's Triage/Fixer, review's Analyzer/FactChecker (not IssueExplorer/Deduplicator/Synthesizer/Styler), respond's Planner. Summary gets none. The excluded agents either do no independent judgment worth recording (Synthesis merges other agents' output; Styler only rewords) or would multiply write contention for no benefit (two Analyzer instances already run in parallel per review).

## Consequences

- Memory persists across runs, containers, and time, confirmed end to end with a live backend, live Postgres, a real signed token, and a real `claude_agent_sdk` model call: a real `AnalyzerAgent` run wrote an institutional-knowledge note on its own judgment, and a second run against the same workspace found it via its own first `view` call and correctly didn't duplicate it.
- Zero-config by design: no opt-in flag, no signing-secret env var. Every workspace gets memory, and every backend instance provisions its own signing secret on first start. There's nothing for an operator to set up or an admin UI to expose.
- The per-workspace entry-count cap (2,000) is enforced check-then-act, not a database constraint; under high concurrency a workspace could exceed it by a small margin. This is accepted: the cap is an explicitly soft, tunable limit, not a security boundary.
- `/internal/memory` is a container-only, bearer-token-authed namespace, but the backend's generic router auto-discovery (the same mechanism that already includes webhook routers) puts it in the public OpenAPI schema and generated frontend SDK regardless. This is not a security issue (frontend session auth can't produce a valid memory token), just a shared architectural quirk that this feature didn't introduce and doesn't need to fix.
- This is a first, deliberately narrow implementation. Not built, intentionally: per-entry access control beyond workspace scoping, size-based eviction beyond the flat per-entry and per-workspace caps. None of it gets built unless real usage shows it's needed.

## Addendum: curation (2026-09)

Real usage showed it was needed for pruning. Three weeks of production memory held 191 entries, and most of them were run logs ("PR reviewed, no findings", rebase narration, "round 4" notes), one file per Sentry issue for the same error, and duplicates: the parallel writers appended rather than updated. Two files had grown past the 16,000-character view truncation, so part of their content never reached a reader.

- **Tighter write rules.** The protocol block now states the test ("would an agent on a *different* task act differently after reading this?"), lists what never to write, and fixes a layout: `<repo>/<topic>.md`, `_workspace/<topic>.md` for knowledge that holds across repos, one `<repo>/sentry-known-noise.md` per repo, files under ~8,000 characters.
- **Soft delete.** `delete` stamps `deleted_at` instead of removing rows. Every query serves live rows only, and uniqueness is a partial index over live rows, so a deleted path can be created again. Storage is cheap; a bad curation call must never lose knowledge for good.
- **A curator.** The backend's `memory` plugin owns its own container backend, watcher and status consumer, plus a scheduler that claims workspaces with entries changed since the curator last saw them (`updated_at > curated_at`), at most once per `cadence_hours` (default 24). Each run is the CLI's `memory-curate` workflow: batches of due entries go to a curator agent with the memory tool and no built-in tools, which deletes, merges, rewrites and moves entries against the same rules the writers follow. A batch is marked curated only once its session finishes, so a failed run leaves its entries due for the next one. A curator run links no issue or PR, so the dashboard doesn't list it. It retries on the next scan rather than through the SCHEDULED path.
- **Impact.** The practical effect is fewer repeated investigations and fewer repeated corrections. A false positive confirmed once by triage doesn't get re-investigated on the tenth occurrence of the same Sentry error; a convention a human corrects once in review doesn't get re-flagged on the next PR. The cost is small and bounded (soft per-workspace caps, no unbounded growth), while the savings compound with every run that reuses existing knowledge instead of rediscovering it.
