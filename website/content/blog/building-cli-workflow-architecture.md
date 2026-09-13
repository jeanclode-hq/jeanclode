---
title: "Inside the JeanClode CLI: A Workflow-First Architecture"
description: "The design behind JeanClode's CLI: why workflows are self-contained plugins instead of one monolithic agent, how adaptors decouple platforms from logic, and how the Claude Agent SDK drives multi-phase pipelines."
publishedAt: "2025-06-05"
readingTime: 8
seoTitle: "Inside the JeanClode CLI: Workflow-First Design"
seoDescription: "Why JeanClode's CLI is a thin router over self-contained workflows rather than one monolithic agent, and how adaptors keep platforms out of the logic."
---

Run `jeanclode https://sentry.io/issues/12345` and a few dozen lines of routing code figure out what to do, then hand off entirely to a self-contained workflow. The CLI itself carries almost no business logic. That's on purpose.

This post is about that architecture: why it's shaped this way, what it costs, and how the Claude Agent SDK fits into it.

## What's wrong with one big agent

The obvious way to build an autonomous coding agent is a single agent with a long system prompt: "You are a code assistant. Given a Sentry error, read the code, find the bug, fix it, open a PR."

That works for a demo. It breaks in production, for a few concrete reasons. Triage context bleeds into fix context, and the agent loses track of what it's actually doing. There's no structured output to route on, so "is this issue actionable?" is buried somewhere in prose instead of being an answer you can branch on. Everything runs sequentially, so triaging four issues takes four times as long as triaging one. A failed fixer takes all your triage work down with it, so retrying from scratch is expensive. And a monolithic trace is thousands of tokens with no phase boundaries, which makes debugging a guessing game.

Splitting the work into explicit phases with a structured contract between them solves all of it at once: the orchestrator handles routing, parallelism, and recovering from a failure in one phase without losing the others.

## The shape of it

```
cli.py (parse args → URLInput, CLIArgs)
  → runner/run.py  find_workflow(url, command) → config, auth, workspace
    → workflows/<name>/runner.py  the workflow class, orchestration only
      ├─ src/activities/<domain>/  deterministic Python: git, PRs, CI, posting
      └─ src/agents/<domain>/      Claude Agent SDK sessions with output schemas
```

Each layer has one job and doesn't leak logic upward. A workflow's own `runner.py` file is thin: it sequences calls into the shared `activities/` and `agents/` packages, organized by domain (`sentry`, `review`, `respond`, `issue`, `git`, `ci_watch`) rather than duplicated inside each workflow. Two different workflows that both need to open a PR or clone a repo call the same activity.

The split between activities and agents is the one that matters most. An activity is plain Python: cloning a repo, opening a PR, polling CI, posting a comment, deciding which issues survive triage. An agent is a model call. Anything decidable deterministically is an activity, and model calls are reserved for judgment.

That line is what makes a run auditable. A triage verdict belongs to the model. What happens as a *result* of that verdict, which repos get worktrees, which PRs get opened, which labels get attached, is code you can read, test, and step through.

### Adaptors: keeping platforms out of the logic

An adaptor wraps one platform (Sentry, GitHub, GitLab) and handles everything platform-specific: matching a URL to that platform, resolving auth (`gh auth token` for GitHub, `glab auth token` for GitLab, `~/.sentryclirc` for Sentry), resolving which repo a Sentry URL maps to, building the environment variables a workflow needs, prefetching the PR diff and discussion into a `.context/` directory so agents read it from a short relative path instead of re-fetching it, and mapping CLI commands to workflow names.

Adding a new platform means writing an adaptor. Workflows never know which one they're running under.

Adding a new workflow to an existing platform is one line: a new entry in the adaptor's command map. The CLI exposes it as a subcommand automatically.

### Workflows: structured pipelines, not prompts

A workflow is a Python class registered with a decorator:

```python
@register
class SentryFixWorkflow:
    name = "sentry-fix"
    triggers = ["*sentry.io/issues/*", "command:sentry"]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        ...
```

`RunContext` carries everything the workflow needs: environment variables, working directory, issue URLs, a dry-run flag, an event emitter for progress. Workflows don't import from the CLI layer. They're self-contained plugins.

`triggers` is what the preflight step matches against. A URL like `https://sentry.io/issues/12345` matches `*sentry.io/issues/*` and routes to `SentryFixWorkflow`. A command like `jeanclode review https://github.com/...` matches `command:review` on the GitHub adaptor, which maps to `code-review`.

This same shape now backs six workflows: fixing Sentry errors, reviewing PRs, writing PR descriptions, resolving GitHub and GitLab issues, handling `@jeanclode-bot` mentions, and an internal smoke test. None of them share a runtime beyond the activities and agents they both happen to need.

### The Sentry fix workflow

This is the most complex workflow, and the clearest case for why phases matter:

```
fetch_sentry_data (deterministic)
  → _triage_all (parallel LLM agents, triage is also the planner)
    → filter_and_route (deterministic)
      → _synthesize (LLM agent, only if batch)
        → _run_group × N (parallel, one fixer session each)
```

Every deterministic step is free of LLM calls. `fetch_sentry_data` is a Sentry API call. `filter_and_route` applies the decision-tree rules (already running, PR open, previously rejected, repo unresolved) without a model in the loop. Triage's structured output feeds routing logic directly, never parsed out of prose.

There's no separate planning agent, and that's deliberate. Triage has to read the source to reach a verdict at all: the difference between "the stack trace names this file" and "this is genuinely broken" is opening the file. That investigation is exactly what a planner would need to redo, so triage writes it down as `findings` instead of discarding it and paying a second agent to re-derive it from a summary.

Parallelism happens in two places: triage agents run concurrently across issues, and group fix pipelines run concurrently across groups. A batch of four issues split into two groups takes about as long as one issue in one group, not four times as long.

Each group gets its own branch, and each repo it targets gets its own worktree on that branch:

```python
wt = create_worktree(branch, ctx=target_ctx, dest=dest)
wt_ctx = ctx.with_cwd(wt.path)
```

Group A's fixer is fully isolated from group B's: different branches, different directories, no shared state. If group B's fixer crashes, group A's PRs are already open.

Inside a single group, it's the opposite: one fixer session spans every worktree. A fix touching a backend and its paired frontend is one change, and splitting it across two sessions that each reason about half of it is how you end up with a PR pair that only makes sense in the author's head.

### The code review workflow

Same pattern, longer chain:

```
IssueExplorer (fetch linked GitHub/GitLab issues for context)
  → Analyzer × 2 (parallel, independent reads of the diff)
    → Synthesizer (merge findings from both)
      → Deduplicator (filter against existing PR discussion)
        → FactChecker (verify each finding against actual code)
          → Guardrail (deterministic, drop anything leaking a secret)
            → Styler (normalize tone and format)
              → post_comments (deterministic, platform-specific)
```

Two Analyzers reading the diff independently is deliberate redundancy: each one misses different things, and the Synthesizer's merged list catches more than either would alone. Everything after the Analyzers is a filter, removing findings that don't clear its bar, and the count that survives to your PR is usually a fraction of what either Analyzer produced on its own.

## The Claude Agent SDK

Each agent is a subclass wrapping a model call with a typed input and structured output:

```python
class TriageAgent(SentryAgent):
    name = "Triage"
    prompt_file = "triage.md"
    allowed_tools = ["Read", "Grep", "Glob", "Bash"]
    output_schema = TriageOutput
    use_memory = True
```

The prompt lives in its own markdown file, readable and editable as prose rather than buried in a class. The output schema is a Pydantic model, which is what lets the next deterministic step route on the result instead of pattern-matching text.

The SDK handles model selection per agent, tool use for the agents that need code access, structured output validated against a JSON schema, streaming for progress events, and retries on transient API errors.

Most agents run on the high tier. The ones that only merge or reformat what another agent already produced, the Synthesizer, Deduplicator, and Styler, drop to a faster, cheaper model, since there's no judgment call there to lose.

### Hooks: guards a model can't argue with

A prompt saying "always push your fix" is a request. Some things need to be a rule instead, so they're `PreToolUse` hooks that check real state and block the agent's turn from finalizing until it's satisfied.

A fixer can't finish without a real commit pushed on top of the run's base, verified against `origin`, not against what the agent claims it did. It can't finish until the repo's own CI is green on that commit, or there's no CI signal to gate on at all; a failure comes back with the failing step's logs, bounded to six rounds. A GitLab reply has to land inside the discussion the mention came from, because `glab`'s flat-note shortcuts silently break threading.

These gate the structured-output call rather than `Stop`, which matters if you're building something similar: a `Stop` hook fires *after* a schema-bound agent has already finalized its turn, so by then it's too late.

## Running locally vs. in a container

Same binary either way. The difference is how it's invoked.

Locally, interactively:
```bash
jeanclode https://github.com/org/repo/pull/12345
```
Rich progress UI, panels, spinners.

In a container, from the backend:
```bash
jeanclode issue1 issue2 --repo org/api issue3 --repo org/billing
```
Repo URLs are passed explicitly because the backend already resolved them. No prompts, JSON structured output to stdout, picked up by the container's log watcher.

`--repo` applies to every issue preceding it up to the last `--repo`, so a batch of three issues across two repos is one command.

## What stays thin

The runner itself does exactly seven things: parse args, load config, resolve the workflow from the URL or subcommand, detect the adaptor and resolve auth, create a workspace and prefetch context, invoke the workflow, and show a UI or emit JSON.

Everything else lives in workflows, activities, and agents. Workflows are testable in isolation with no CLI scaffolding, portable to any runner, and deployable on their own as the container entrypoint.

---

The full source is on [GitHub](https://github.com/jeanclode-hq/jeanclode). The workflow registration pattern, agent base classes, and adaptor interface are documented in the CLI's CLAUDE.md.
