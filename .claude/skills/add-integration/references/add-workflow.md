# Adding a New Workflow

A workflow is a new pipeline (code review, dependency update, incident
response) Jeanclode can run — as opposed to a new *source* (add-source.md),
which is a new place issues come from. **A workflow is a Python class, not
a Claude Code plugin.** There is no `plugins/<name>/`, no
`.claude-plugin/plugin.json`, no `SKILL.md` orchestrator, no
`${CLAUDE_SKILL_DIR}` scripts, and no LLM-driven step sequencing. The
orchestrator is plain, deterministic Python; only individual bounded turns
are LLM-backed.

```
cli/src/workflows/<name>/runner.py   the orchestrator — a `Workflow`, `@register`-ed
cli/src/agents/<name>/*.py           BaseAgent subclasses, one per LLM turn
cli/src/prompts/<name>/*.md          plain prompt text for each agent (Jinja-rendered)
cli/src/activities/<name>/*.py       deterministic Python: git, API calls, posting
```

Read `cli/src/workflows/code_review/` (closest existing analog — multiple
agents, parallel + sequential stages, posts results back to the platform)
or `cli/src/workflows/issue_resolve/runner.py` (simpler: two stages,
extensive module docstring explaining every design decision — read the
docstring, it's the best worked example of *why* the pipeline is shaped
the way it is) before writing anything.

**Sequencing is Python, not an LLM following instructions.** The
workflow's `run()` method *is* the determinism — a step cannot be silently
skipped. Put plain logic in the workflow function; call an agent only for
the parts that require judgment.

## Step 1: The Workflow Class

```python
@register
class MyWorkflow:
    name: ClassVar[str] = "my-workflow"
    description: ClassVar[str] = "What this workflow does"
    triggers: ClassVar[list[str]] = [
        "command:my-workflow",           # CLI subcommand
        "github.com/*/pull/*",           # URL globs, fnmatch-style
    ]

    async def run(self, ctx: RunContext) -> WorkflowResult:
        ...
```

`register` (from `src.workflows.base`) validates the class has `name`,
`description`, `triggers`, `run` and adds it to the global `WORKFLOWS`
registry. `find_workflow(url=..., command=...)` matches an explicit
`command:<name>` trigger first, then falls back to `fnmatch`-ing URL
globs. If the workflow is the *only* handler for a bare CLI subcommand
with no adaptor behind it (like the `echo` smoke workflow), it also needs
to show up in `command_names()` — check `src/workflows/base.py` for how
that's derived before assuming a bare `jeanclode <name>` invocation works.

Add to `LOCAL_ALLOWED_WORKFLOWS` in `cli/src/runner/run.py` **only** if
the workflow makes sense as a one-shot terminal command with no backend
follow-up loop. Anything relying on CI-gating, labeling, or the
review/respond loop (see root `CLAUDE.md`'s "Ready notice" section) must
stay container-only — it depends on the backend's async webhook loop to
finish what one run starts.

## Step 2: Agents

One `BaseAgent` subclass per LLM turn (`cli/src/agents/<name>/*.py`).
Each sets, at minimum, `prompt_file` and `output_schema`; `allowed_tools`
scopes what the turn can do. Relevant opt-in flags on `BaseAgent`
(`cli/src/agents/base.py`) — check which your agent actually needs, they
are not defaults:
- `use_third_party_skills` — lets the agent consult org-installed
  marketplace skills via the `Skill` tool, a consultation mechanism for
  third-party content, not how Jeanclode's own workflows are built. If
  enabled, the agent's own prompt file needs a guardrail section naming
  which output fields a third-party skill may influence.
- `use_mcp_connectors` — org-registered MCP servers.
- `use_memory` — the cross-run memory tool (ADR-009). Enable only on
  judgment-heavy agents (triage/fixer-shaped), not on agents that just
  merge or reword other agents' output.
- `use_continuity` — warns the agent that surrounding comment/discussion
  history may include prior turns from other Jeanclode agents on the same
  thread.

`output_schema` must be a Pydantic model whose fields have **no
`default=`** if you want the CLI's strict-schema derivation to actually
constrain generation — see the long comment on `BaseAgent._strict_schema`
for the production incident (`jc-sentry-1887793`) that made this a hard
rule, not a style preference. A schema using `$ref`/`$defs` (nested
models) is sent unmodified and falls back to permissive validation.

## Step 3: Prompts

Plain Markdown under `cli/src/prompts/<name>/<role>.md` — **no YAML
frontmatter, no tool-orchestration directives**. Rendered with Jinja
against the agent's input model:
`Template(prompt_text).render(**agent_input.model_dump())`. Reference the
input model's fields directly (`{{ issue_body }}`).

## Step 4: Activities

Deterministic Python under `cli/src/activities/<name>/*.py`, decorated
with `@activity` (`cli/src/activities/decorator.py`) so the progress UI
gets `ActivityStart`/`ActivityEnd` events automatically:

```python
@activity(name="Fetching PR context")
def fetch_pr_context(url: str, *, ctx: RunContext) -> PrContext: ...
```

The decorated function must take `ctx: RunContext` as a keyword argument.
Reuse existing activities where the behavior is identical across
workflows — `cli/src/activities/git.py`'s `open_pr`/`create_worktree`/
`push_branch`/`attach_label` are workflow-agnostic and used by both
`sentry_fix` and `issue_resolve`.

### Pre-fetched context

For a workflow operating on a fixed payload (a diff, an issue body, a
thread), fetch it once via an activity before invoking any agent, and pass
it through the agent's input model — don't have the agent fetch it itself
via a tool call mid-turn. `issue_resolve`'s `fetch_issue_context` +
`IssueContext` (passed into `TriageInput`) is the pattern.

## Step 5: Display Integration

**Required** — without it the CLI trail shows raw class names and no step
transitions. Update `cli/src/adaptors/<platform>/display.py` for each
adaptor the workflow can run under:

- `_AGENT_LABELS` — keyword (substring of the agent's `name` ClassVar) →
  step label shown when that agent starts.
- `_AGENT_PREFIX` — keyword → trail prefix for that agent's tool calls.
  Parallel launches of the same agent auto-index (`Reviewer[0]`,
  `Reviewer[1]`).

Don't copy `_SCRIPT_LABELS` / `_invokes_script` if you see them in an
existing `display.py` — they match nothing a new workflow produces, since
activities are called directly by the Python workflow, never invoked by
an agent via Bash.

**Gating**: step transitions must fire only when the tool-call event's
`parent_tool_use_id is None` (orchestrator level) — a subagent that
happens to read a source file containing a similar string shouldn't
trigger a spurious transition.

**Parallel result aggregation**: if multiple agent instances share one
step label (two analyzers running concurrently), aggregate their result
counts into a per-step counter that resets on label change — don't let
the last one's `tracker.done(detail=...)` overwrite earlier counts.

**JSON parsing must be lenient**: the SDK can wrap script/tool stdout with
extra text. Use a fallback chain — direct `json.loads` → fenced ```` ```json ````
block → first balanced `{...}` span. See `_safe_json` in
`cli/src/adaptors/github/display.py`.

## Step 6: Structured Log Output

Container-mode runs must emit `[JEANCLODE:RESULT]` / `[JEANCLODE:ERROR]`
(`cli/src/output.py`'s `emit_result`/`emit_error`) so the backend watcher
can parse the outcome without relying on exit code alone. This happens
once, at the runner level, from the `WorkflowResult` your `run()` method
returns — most workflows don't call `emit_result` directly.

### Result schema convention

When a script/activity's output represents a semantic state
(success/failure, lgtm/comments-posted, merged/skipped), expose it as a
**named boolean flag** alongside any counts — don't conflate states into
a bare count. An LGTM-posting step should return
`{"posted": 0, "lgtm": True, ...}`, not `{"posted": 1}` (which reads as a
real finding having been posted).

## Step 7: Backend, Sandbox, Frontend

Same requirements as a new source — see add-backend.md, sandbox-proxy.md,
and add-frontend.md if the workflow needs tenant-facing settings (most
workflow-only additions reuse an existing source's org settings and don't
need new frontend work; a genuinely new trigger surface might).

## Step 8: Tests

- `cli/tests/test_<name>_workflow.py` — workflow orchestration
- `cli/tests/agents/test_<name>_*.py` — per-agent behavior where it's
  worth isolating (schema handling, prompt rendering)
- Backend tests mirroring the relevant plugin's test suite

## Checklist

- [ ] Workflow class registered via `@register`, with `name`/`description`/
      `triggers`; added to `LOCAL_ALLOWED_WORKFLOWS` only if it has no
      backend-loop dependency
- [ ] Agents are `BaseAgent` subclasses under `cli/src/agents/<name>/`
- [ ] Prompts are plain Markdown under `cli/src/prompts/<name>/`, Jinja-
      rendered against the input model
- [ ] `output_schema` fields have no `default=` (or the schema
      legitimately can't be strictified and that's accepted deliberately)
- [ ] Deterministic logic lives in the workflow function or an
      `@activity`, not folded into an agent's judgment
- [ ] Pre-fetched context passed through the agent's input model, not
      fetched by the agent mid-turn
- [ ] Display layer updated for every adaptor the workflow runs under
      (`_AGENT_LABELS`, `_AGENT_PREFIX`), gated on `parent_tool_use_id is None`
- [ ] Parallel-agent result counts aggregated, not overwritten
- [ ] JSON parsing lenient (direct → fenced → balanced-span fallback)
- [ ] Workflow's `run()` return value backs `[JEANCLODE:RESULT]` /
      `[JEANCLODE:ERROR]` correctly
- [ ] Result schemas use named boolean flags for semantic states
- [ ] Backend/sandbox/frontend support added if the workflow needs it
- [ ] `make qa` passes
- [ ] `make test` passes
