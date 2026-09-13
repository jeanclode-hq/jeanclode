---
title: "Giving Agents Memory That Actually Persists"
description: "Why a workspace-scoped memory system fits JeanClode's multi-tenant agents better than Anthropic's built-in memory tool, and what changes when an agent can recall what a previous run already learned."
publishedAt: "2026-08-19"
readingTime: 8
seoDescription: "Why JeanClode's agents use a workspace-scoped memory store rather than Anthropic's memory tool, and what changes when a run can recall the last one."
---

Every JeanClode run used to start with the current state of a repo and nothing else. An agent that spent three minutes confirming a Sentry error was a known false positive spent the same three minutes on the next occurrence of that error. A review agent corrected by a human on a repo-specific convention had no way to carry that correction into the next review of that repo. None of that knowledge persisted, because nothing existed to hold it.

Now something does: a durable, workspace-scoped store agents read from and write to across runs. This post is about how it's built, and about the checks in place that confirm it behaves the way it's meant to, not just that its tests pass.

## Why the built-in tool doesn't fit

Anthropic ships a `memory_20250818` tool for exactly this shape of problem, but two things rule it out for direct use here.

First, it's client-side by design: the caller supplies the storage. Anthropic's reference implementations are single-user, a local filesystem directory. JeanClode is multi-tenant: many workspaces, many sandboxed containers running concurrently, no shared filesystem between any of them. Bringing your own storage means building a backend regardless of which tool shape gets picked.

Second, and more interesting, the tool's framing doesn't match this use case. When it's present in a request, the API auto-injects this into the system prompt:

```text
IMPORTANT: ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE.
MEMORY PROTOCOL:
1. Use the `view` command of your `memory` tool to check for earlier progress.
2. ... (work on the task) ...
   - As you make progress, record status / progress / thoughts etc in your memory.
ASSUME INTERRUPTION: Your context window might be reset at any moment, so you
risk losing any progress that is not recorded in your memory directory.
```

That's a session-recovery mechanism: a progress log for surviving a context reset in the middle of one long task. It's close to the opposite of what's needed here, which is durable knowledge shared across separate runs, days apart, in different containers, for different issues. An agent following that prompt literally would treat memory as a scratch pad and fill it with "step 3 of 7 complete" instead of "this error is a known false positive, here's why."

`claude_agent_sdk`, the Claude Agent SDK JeanClode's CLI runs on, has no built-in memory tool at all (true of both the pinned version and the latest). What it does provide is `create_sdk_mcp_server` and `@tool` for defining custom in-process tools, which is what the memory system is built on: backend storage, a credential path into it, a CLI-side tool, and a system prompt that keeps the one part of Anthropic's framing that transfers and drops the rest.

## How it's structured

Storage is scoped to the workspace, not the repo: one workspace can span several repos, and a repo is just a path prefix inside the store (`backend-repo/notes.md`, `frontend-repo/notes.md`). Six operations match Anthropic's own contract, so a model's trained expectations of "the memory tool" still hold: `view`, `create`, `str_replace`, `insert`, `delete`, `rename`.

Reaching it from a sandboxed container needs no infrastructure beyond what already exists: it goes through the same security-proxy sidecar every other credential already uses. At dispatch time, the backend mints a short-lived, signed token scoped to that workspace and that execution, and hands it to the sidecar. The agent process never sees or constructs an `Authorization` header, same as it never sees a raw GitHub token. `workspace_id` lives only inside the verified token, never a client-supplied field, so a prompt-injected agent can't reach a different workspace's memory by changing a request parameter: there's no parameter to change.

The prompt block agents get, once memory is enabled for their workspace:

```
ALWAYS VIEW YOUR MEMORY DIRECTORY FIRST, BEFORE DOING ANYTHING ELSE.

This memory tool is durable, cross-run storage for this workspace's repos -
NOT scratch space for this run. Anything you write here is still there the
next time any agent runs against this workspace, potentially days or weeks
from now and in a completely different container.

Use it for institutional knowledge that would otherwise be re-discovered the
hard way every run [...]. Do NOT use it to track progress within this run -
that's what your own reasoning and this run's output are for.
```

Same leading instruction as Anthropic's reference tool (view first, no exceptions), paired with a different reason for it.

## Correctness under concurrent writes

Multiple agents can write to the same workspace's memory at once, which creates two failure modes the design accounts for.

The first is a lost update: `str_replace` and `insert` do a read-modify-write, so without locking, two agents editing the same file at nearly the same moment could have one edit silently overwrite the other. A `SELECT ... FOR UPDATE` on the read prevents this: the second writer blocks until the first commits, then re-reads the already-updated content instead of clobbering it.

The second is a naming collision: nothing stops a file and another entry's path prefix from sharing a name, `notes` as a file and `notes/x.md` as a separate entry. A `view` on the file would then permanently hide `notes/x.md`, which still exists and would still get swept up by delete or rename, just invisible to the one command an agent would actually use to discover it. `create` and `rename` now reject both directions of that collision. Neither issue shows up in a straightforward "does create work, does view work" test suite; both surfaced under adversarial code review, which is the case for having one.

## Confirming it works in practice

Unit tests confirm the logic is correct in isolation. They don't confirm that a real model, given the real tool, uses it the way it's designed to be used. End-to-end verification covers that: a live backend, a live Postgres database, a live signed token, and the real `AnalyzerAgent` (the same one that reviews real pull requests) pointed at a real diff from this repository's own history.

The agent's first tool call is `view` on its memory root. On an empty, new workspace, it investigates the actual code rather than trusting the PR description, independently confirms a design decision is intentional rather than a bug, and, without being told to, writes a note:

> *"`db_get_memory_entries_by_paths` has no `.with_for_update()`... This is not a bug, do not flag it again. Why it's safe: [cites the exact code paths and line numbers it read]..."*

Postgres shows the row: real content, cited line numbers matching the actual file. Run a second time against the same workspace, on a cheaper model, the agent's `view` call finds the existing note, reads it, and correctly skips writing a duplicate. Postgres still shows exactly one row.

That's the promise of the feature demonstrated rather than asserted: something one model learned in one run, in one container, is still there, and gets used, in a completely separate run.

## Where it's enabled, and what changes because of it

Memory is opt-in per agent, not blanket-enabled across every pipeline. It's live on agents that do independent judgment worth remembering: the Sentry and issue triage agents (which double as the planners), both fixers, the code review Analyzers and FactChecker, and the respond planner. It's deliberately off on agents that only merge or reformat other agents' output, where there's nothing new to record. Per workspace, though, there's no opt-in at all: every workspace gets memory, and the signing secret that authenticates it is generated by the backend itself on first start, nothing for an operator to configure.

The effect compounds over time rather than showing up in any single run. A false positive confirmed once stays confirmed. The next ten times that Sentry error fires, triage doesn't re-open the same investigation. A convention a reviewer corrects once stays corrected across every future review of that repo, instead of getting re-flagged and re-explained. None of it changes what a single run looks like from the outside, but it changes how much of that run is genuinely new work, versus work already done somewhere else, on some other day, by some other container.

---

Full design rationale, including the reasoning against Anthropic's reference framing and the concurrency fixes in detail, is in [ADR-009](https://github.com/jeanclode-hq/jeanclode/blob/main/docs/adr/009-agent-memory-system.md). The memory system sits alongside the same [multi-agent pipeline](/docs/architecture/overview) and [autonomous loop](/docs/architecture/autonomous-loop) that already power JeanClode's other workflows; see [Agent Memory](/docs/architecture/memory) for the full reference.
