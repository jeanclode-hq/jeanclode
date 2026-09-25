# CLI

Thin runner around Python-driven workflows. A URL (or an explicit
subcommand) selects the workflow; the workflow orchestrates deterministic
*activities* and LLM-backed *agents* through the Claude Agent SDK.

Locally it renders a Rich progress UI; in a container it emits structured
JSON on stdout that the backend's watcher consumes.

## Usage

```bash
# Code review on a GitHub PR (default for a PR URL)
jeanclode https://github.com/org/repo/pull/123

# PR summary instead of review
jeanclode summary https://github.com/org/repo/pull/123

# GitLab MR review (gitlab.com or self-hosted)
jeanclode https://gitlab.com/group/project/-/merge_requests/42
jeanclode review https://gitlab.example.com/group/project/-/merge_requests/42

# From source
cd cli && uv run jeanclode https://github.com/org/repo/pull/123
```

Flags: `--repo`, `--related-repo` (repeatable), `--dry-run`, `--debug`,
`--model`. `jeanclode config` runs interactive setup.

`code-review`, `pr-summary` and `echo` are the only workflows that run from
a bare terminal invocation (`LOCAL_ALLOWED_WORKFLOWS` in
`src/runner/run.py`); everything else — `sentry-fix`, `issue-resolve`,
`jeanclode-respond` — only runs as the container entrypoint
(`JEANCLODE_CONTAINER_MODE`), since their CI-gate, labeling and the review
loop depend on the backend's async webhook loop finishing what one run
starts, which a bare terminal invocation has no way to receive.

## Architecture

```
cli.py (parse_args → URLInput, CLIArgs)
  -> main.py (cli_entry / async main)
    -> runner/run.py
         find_workflow(url=…, command=…)   # workflows/base.py registry
         runner/preflight.py               # config, auth, clone, .context, env
         workflow.run(ctx)                 # RunContext carries cwd/workspace/env
           -> activities/  (deterministic Python: git, PRs, CI, posting)
           -> agents/      (Claude Agent SDK sessions, prompts + output schemas)
```

### Workflow selection

`src/workflows/base.py` holds a global registry. A workflow class declares
`name`, `description` and `triggers`, and `@register` puts it in
`WORKFLOWS`. Triggers are either URL globs (`*sentry.io/issues/*`) or
`command:<name>` literals; an explicit subcommand wins over a URL match.
Adding a workflow means adding a package under `src/workflows/` with a
registered class — nothing else in the runner changes.

`src/adaptors/` still owns the *platform* side: URL parsing, auth
resolution (env / `gh auth token` / `glab auth token` / `~/.sentryclirc`),
repo-URL resolution, SHA/head-ref lookup, `.context/` prefetch, and the
env vars that platform's tools need.

### Workflows

| Workflow | Shape |
|---|---|
| `sentry_fix` | fetch → parallel triage (triage *is* the planner) → deterministic filter/route → synthesis when several issues are actionable → per group: worktree + PR per target repo, one fixer session across them |
| `code_review` | IssueExplorer → 2× Analyzer (parallel) → Synthesizer → Deduplicator → FactChecker → Guardrail → Styler → post inline comments |
| `issue_resolve` | triage (explores the codebase, findings double as the plan) → fixer → PR per repo, opened only once that repo has a real pushed commit |
| `pr_summary` | Summarizer → Parser, File Summarizer started 3s after the Summarizer → rewrite the PR/MR description with a collapsed per-file dropdown |
| `jeanclode_respond` | one planner agent acting via Bash, plus two deterministic post-turn checks against provider state |
| `_smoke/echo` | internal smoke test, no external calls |

### Activities vs agents

- **Activities** (`src/activities/`) are plain Python: `git`, `gh`, `glab`,
  CI polling, comment posting, label attachment, secret scanning. They are
  decorated with `@activity` so the event bus can render them, and they
  never call a model.
- **Agents** (`src/agents/`) each own a prompt file under `src/prompts/`
  and (almost always) a Pydantic `output_schema`. `BaseAgent` handles
  option building, hooks, memory, third-party skills, MCP connectors and
  structured-output parsing.
- Agents reading the same large context can put it in a shared
  `system_prompt_file`. With identical tools and `output_schema`, a request
  started once the other's response has begun reuses its prompt cache —
  `pr_summary`'s Summarizer and File Summarizer do this. The output schema counts because
  the CLI turns it into a tool, and tools sit ahead of the system prompt.

Anything a workflow can decide deterministically belongs in an activity.
Model calls are for judgment.

### Hooks

`src/agents/hooks.py` holds the deterministic guards wired into agent
sessions:

- `require_pushed_fix_hook` — a fixer can't end its turn without a real
  commit pushed on top of the run's placeholder SHA.
- `require_ci_pass_hook` / `require_pushed_and_ci_pass_hook` — the repo's
  own CI has to be green (or absent, or already red on the default branch)
  before the fixer finalizes. Bounded at six rounds; a bypass marker file
  lets the agent opt out when the failure isn't its to fix.
- `require_threaded_gitlab_reply_hook` — keeps a respond reply inside the
  discussion the mention came from.

`require_pushed_fix_hook` is a real `Stop` hook. The CI ones are not: they
gate `PreToolUse` on the schema-bound structured-output call instead,
because a `Stop` block lands after a schema-bound agent has already
finalized its turn and the CLI drops it.

### Runtime

`src/runtime/` carries the cross-cutting pieces: `RunContext` (cwd,
workspace, related repos, dry-run, notify list, memory flag, LLM options),
the event bus and event types, bot-account detection, org MCP connectors,
and the ready-notice marker.

### Fixer LLM choice

`JEANCLODE_LLM_OPTIONS` (see `src/runtime/llm_options.py`) lists the
credentials the backend loaded for this run, the default first. When it's
set, agents with `choose_fixer_llm` (both triage agents) get a prompt block
listing them plus the loaded skills, and fill `fixer_llm_credential` /
`fixer_llm_tier` / `fixer_llm_reason`. `resolve_fixer_llm` maps that onto a
configured option (unknown names keep the default, with a note);
`merge_choices` picks one per Sentry group; `apply_fixer_llm` retargets only
the fixer's `RunContext` (model, env, credential id). Every other agent
keeps the default. When unset, nothing about a run changes.

A session that fails carries its `JEANCLODE_LLM_CREDENTIAL_ID` on the
exception (`llm_credential_id`), so a 429 in a retargeted fixer stales the
credential it actually ran on.

## Testing

```bash
cd cli && uv run pytest -v
```

`tests/eval/` holds LLM-judged agent evals; they are slower and hit the
API, so they're separate from the unit suite (`make eval`).
`tests/eval/agents/test_triage_fixer_llm.py` covers the fixer LLM choice.

## Guidelines

- Follow existing code style and patterns
- Add type hints for function signatures
- Write docstrings for public classes/functions
- Tests use flat functions, no classes. Use the available fixtures.
- The runner stays thin — orchestration belongs in a workflow, determinism
  in an activity, judgment in an agent
