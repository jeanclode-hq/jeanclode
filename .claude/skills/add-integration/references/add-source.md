# Adding a New Source

A source is an external system Jeanclode ingests issues/triggers from
(Sentry, GitHub, GitLab today; Jira, Linear, PagerDuty are candidates).
There is **no plugin-marketplace layer for Jeanclode's own workflows** —
no `plugins/` directory, no `.claude-plugin/plugin.json`, no `SKILL.md`
orchestrator, no `${CLAUDE_SKILL_DIR}` scripts. The shape is plain Python:

```
cli/src/adaptors/<name>/        URL parsing, auth, repo resolution (this file)
cli/src/workflows/<name>/       async orchestration (see add-workflow.md)
cli/src/agents/<name>/          BaseAgent subclasses (LLM turns)
cli/src/prompts/<name>/         plain prompt .md files (Jinja-rendered, no frontmatter)
cli/src/activities/<name>/      deterministic Python (git, API calls, PR posting)
```

## Decide the fix-dispatch shape first — this is not "does it host git"

Two independent questions decide almost every step below:

1. **Does each item stand alone, or does it need batching?**
   - **Discrete-ticket** — a Jira ticket, a Linear issue, a GitHub/GitLab
     issue: one human-authored unit of work. Triage investigates the
     codebase and *is* the plan, one fixer session, one PR, no grouping
     with other tickets. **Template: `issue_resolve`**
     (`cli/src/workflows/issue_resolve/`, `cli/src/agents/issue/`,
     `cli/src/activities/issue/`). This is the right template for
     Jira/Linear/PagerDuty.
   - **Error-monitoring / batch** — Sentry, and the same would apply to a
     future Rollbar/Bugsnag/Datadog-Error-Tracking/Honeybadger addition:
     many machine-generated events that often share one root cause, so a
     windowed batch dispatcher (ADR-006) groups them before triage, and a
     synthesis stage groups actionable ones by shared cause before a
     single fix targets several at once. **Template: `sentry_fix`**
     (`cli/src/workflows/sentry_fix/`, `cli/src/agents/sentry/`,
     `cli/src/activities/sentry/`).
2. **Does the source also host git repos** (and therefore need
   review/summary/respond, not just a fix pipeline)? Only GitHub/GitLab
   today. Relevant only for a literal git-hosting addition (Bitbucket) —
   it's orthogonal to question 1: Bitbucket would still be
   discrete-ticket-shaped for its own issues (each is its own PR, same as
   GitHub/GitLab), it would just *also* need the full multi-skill
   `review`/`summary`/`respond`/`issue-resolve` adaptor surface and
   joining every hardcoded `provider == "github"` / `"gitlab"` branch.

**For Jira/Linear/PagerDuty: extend `issue_resolve`, don't build a
`sentry_fix`-shaped pipeline.** This is more than routing to an existing
workflow — read the wrinkle below before assuming it's a one-line change.

## The wrinkle: `issue_resolve` conflates "issue source" with "git host"

`cli/src/workflows/issue_resolve/utils.py::parse_issue_url_info` parses a
GitHub or GitLab issue URL and returns `(provider, repo, issue_number)` —
`repo` comes directly out of the URL (`owner/repo` for GitHub,
`host/project/path` for GitLab), because a GitHub/GitLab issue URL
*embeds* the repo it belongs to. `provider_to_platform` then assumes the
issue's provider IS the git platform the PR gets opened on
(`repo_url_from_parts`, `cli/src/activities/git.py` calls, `gh`/`glab`
choice in `cli/src/activities/issue/fetch.py` and `comment.py` all key off
this one `provider` string).

**A Jira ticket URL (`https://org.atlassian.net/browse/PROJ-123`) has no
embedded git repo at all** — only a Jira project key, which maps to a git
repo through Jeanclode's own `RepositoryMapping` (backend, resolved
server-side), not through URL parsing. This means "issue source" and "git
platform hosting the fix" have to become two separate concepts for Jira,
where today they're one (`provider`) because GitHub/GitLab issues make
them identical:

- **Fetching the ticket / posting a resolution comment** — needs a Jira
  API call, not `gh`/`glab`. This is what
  `cli/src/activities/issue/fetch.py::fetch_issue_context` and
  `comment.py::post_issue_comment` branch on today (`if provider ==
  "github"` / else gitlab) — add a `"jira"` branch here that calls a Jira
  client instead.
- **Opening the PR / pushing the branch / attaching labels** — still needs
  a real git platform (github or gitlab), resolved from whichever repo
  the ticket's project maps to, not from the string `"jira"`. Everywhere
  `provider_to_platform`/`repo_url_from_parts` is called, the input has to
  become "the resolved repo's actual host," not "the issue's own
  provider."

Concretely: `parse_issue_url_info` needs a Jira branch that recognizes the
URL shape and extracts `(project_key, ticket_number)`, then a lookup
(against the backend, or against data the container was dispatched with)
resolves that project to a `(git_platform, repo)` pair via
`RepositoryMapping` — there is no local parsing shortcut, unlike
GitHub/GitLab. Everything downstream of that resolution (`create_worktree`,
`push_branch`, `open_pr`, `attach_label`, CI-gate hooks) already takes a
platform + repo and doesn't care where they came from, so it needs no
changes. Only the "how do we get from the issue's own URL/id to
`(platform, repo)`" step is source-specific.

## Path A: Discrete-ticket source (Jira, Linear, PagerDuty)

### Step 1: CLI Adaptor

Create `cli/src/adaptors/<name>/` implementing the `Adaptor` Protocol
(`cli/src/adaptors/base.py`, matches `sentry/adaptor.py` and
`github/adaptor.py`):

```python
class Adaptor(Protocol):
    name: str                     # "jira"
    default_command: str          # command used when none given on the CLI
    skills: dict[str, str]        # CLI subcommand -> workflow name, e.g. {"jira": "issue-resolve"}

    def matches(self, url: str) -> bool: ...
    def select_command(self, url: str) -> str: ...       # usually `return self.default_command`
    def resolve_auth(self) -> str | None: ...
    def resolve_repo_url(self, issue_url, token, *, repo_override=None) -> str | None: ...
    def fetch_sha(self, url: str, token: str) -> str | None: ...        # None — always default branch
    def fetch_head_ref(self, url: str, token: str) -> str | None: ...   # None — always default branch
    def fetch_context(self, repo_dir: Path, url: str, token: str) -> None: ...  # no-op
    def build_env(self, token: str, issue_urls: list[str]) -> dict[str, str]: ...
    def build_prompt(self, issue_urls: list[str]) -> str: ...
    @property
    def display(self) -> Display: ...
```

`skills` routes to `"issue-resolve"` — the existing workflow name, since
you're extending it rather than creating a new one. `resolve_repo_url`
has no native mapping to fall back on (unlike Sentry's code-mappings API)
— it resolves via the backend's `RepositoryMapping` or returns `None` and
relies on a `--repo` override.

- `auth.py` — resolve the token: env var first (`JIRA_API_TOKEN`), config
  file fallback, else `None` (mirrors `sentry/auth.py`). The env var name,
  the token type (API token vs OAuth bearer vs basic-auth-with-email),
  and the actual auth header shape the API expects are Jira's, not
  Sentry's — verify them against Jira's current API docs rather than
  copying Sentry's shape by pattern-match.
- `client.py` — thin pre-flight client (token validation, project lookup)
  only. Full ticket data fetching is an activity (Step 3), not here. The
  endpoint paths, auth header, pagination scheme, and response shape must
  come from the provider's current API reference — fetch and read it
  before writing a single request; don't infer REST conventions from
  Sentry's or GitHub's client and assume they transfer.
- `display.py` — implement the `Display` protocol
  (`cli/src/adaptors/display.py`): `on_agent_start`, `on_tool_call`,
  `on_tool_result`. Copy `sentry/display.py`'s structure for agent-name →
  step-label mapping.

### Step 2: Register the Adaptor

```python
# cli/src/adaptors/registry.py
from src.adaptors.jira.adaptor import JiraAdaptor

_ADAPTORS: list[Adaptor] = [
    SentryAdaptor(),
    GithubAdaptor(),
    GitlabAdaptor(),
    JiraAdaptor(),  # NEW
]
```

### Step 3: Extend `issue_resolve`, don't fork it

Generalize, rather than duplicate:

1. `cli/src/workflows/issue_resolve/utils.py::parse_issue_url_info` —
   add a branch recognizing Jira ticket URLs, extracting
   `(project_key, ticket_number)` and resolving the mapped
   `(git_platform, repo)` pair (see "The wrinkle" above — this is not a
   local string-parsing operation for Jira the way it is for
   GitHub/GitLab).
2. `cli/src/activities/issue/fetch.py::fetch_issue_context` and
   `comment.py::post_issue_comment` — add a `provider == "jira"` branch
   that calls a Jira API client instead of `gh`/`glab`.
3. `provider_to_platform` / `repo_url_from_parts` — these need to operate
   on the *resolved git platform*, not the issue's own provider string,
   once a third provider exists. Audit every call site before assuming
   the existing two-way logic still holds.
4. `cli/src/agents/issue/` (`TriageAgent`, `IssueFixerAgent`) and their
   prompts need no changes — they already operate on `IssueContext`
   (title, body, comments) and `target_repos`, independent of where the
   ticket came from.

If, on inspection, Jira's shape diverges enough that shoehorning it into
`issue_resolve`'s single `run()` method becomes awkward (e.g. a
genuinely different triage flow), a separate `jira_resolve` workflow that
still reuses `cli/src/agents/issue/` and `cli/src/activities/issue/` is
the fallback — but exhaust the "generalize `provider ==` branches" option
first, since duplicating the workflow duplicates the CI-gate hook wiring,
worktree/multi-repo handling, and PR-lazy-open logic that `issue_resolve`
already got right (see its `runner.py` module docstring for why it's
shaped the way it is).

### Step 4: Structured Log Output

No change needed if extending `issue_resolve` — it already emits
`[JEANCLODE:RESULT]` / `[JEANCLODE:ERROR]` (`cli/src/output.py`) from its
`WorkflowResult`.

## Path B: Error-monitoring / batch source

Only take this path for a source that genuinely emits many
machine-generated, often-duplicate events needing windowed grouping
(a Rollbar/Bugsnag/Datadog-Error-Tracking/Honeybadger-shaped addition) —
not for a ticket tracker.

1. `cli/src/workflows/<name>_fix/runner.py` — an async `Workflow` class,
   `@register`-ed, `triggers` including `command:<name>`.
   `sentry_fix/runner.py` is the full reference: fetch → triage (which
   also plans — no separate plan stage) → synthesis (grouping by root
   cause) → per-group worktree + draft PR + fixer session → CI-gate →
   label.
2. `cli/src/agents/<name>/` — `BaseAgent` subclasses per LLM turn (triage,
   fixer, synthesis). `output_schema` fields need no `default=` — see
   `_strict_schema` in `agents/base.py`.
3. `cli/src/prompts/<name>/*.md` — plain prompt text, Jinja-rendered
   against the agent's input model.
4. `cli/src/activities/<name>/` — deterministic Python, `@activity`-
   decorated. Full event-data fetching happens here (`sentry/fetch.py` is
   the template), not in the adaptor.

Backend dispatch for this path is a polling batch dispatcher
(add-backend.md's Step 5) — this is the piece that has no equivalent on
Path A.

## Both paths: Backend, Sandbox, Frontend

**Every source that dispatches containers needs all three**, regardless
of which path above it took:

- Backend: [add-backend.md](add-backend.md) — a full `BasePlugin`
  subclass (client + dispatch + watcher together, following
  `SentryPlugin`), an `Organization` row (there is one shared table for
  every provider, not a per-provider table), a connection endpoint, and a
  webhook router if the source has one. The dispatch model differs by
  path — see add-backend.md's Step 5 for which.
- Sandbox / security-proxy: [sandbox-proxy.md](sandbox-proxy.md) —
  unconditional. Add the source's API host(s) as `UpstreamCredential`
  entries, cover SaaS regional hosts up front if applicable, resolve
  self-hosted hosts from the org row, and add any non-standard credential
  header to `STRIPPED_HEADERS` in `security-proxy/proxy.py`.
- Frontend: [add-frontend.md](add-frontend.md) — there is no generic
  "integrations" component; every provider's connect form, overview
  list, and settings panel is bespoke Vue, and several places hardcode a
  provider union that must be extended in lockstep. Check
  `frontend/app/components/onboarding/StepConnectors.vue` first — it
  already lists a `jira` entry with `comingSoon: true`, which may be
  exactly the spot this work is meant to complete.

## Write Tests

- `cli/tests/adaptors/test_<name>.py` — auth resolution, URL matching,
  repo resolution
- Path A: extend `cli/tests/test_issue_resolve_workflow.py`-equivalent
  coverage for the new provider branch
- Path B: `cli/tests/test_<name>_workflow.py` / agent tests
- Backend: watcher/consumer/dispatch tests mirroring the sentry plugin's
  test suite

**Every fixture — URL samples, API response bodies, webhook payloads —
must come from the provider's current documentation or a real captured
request, never be typed from memory.** A CLI URL-parsing regex or a
mocked API response shaped from assumption will pass its own
self-consistent test and still fail the moment a real URL or response
doesn't match the guessed shape. Fetch and read the provider's API
reference for the exact URL formats, response field names, and pagination
convention before writing the test that asserts them.

## Checklist

- [ ] Decided explicitly which axis applies: discrete-ticket (Path A,
      extend `issue_resolve`) vs error-monitoring/batch (Path B, new
      `sentry_fix`-shaped workflow) — and separately, whether the source
      also hosts git repos
- [ ] Adaptor implements every method of the `Adaptor` protocol, including
      `fetch_sha`/`fetch_head_ref`/`fetch_context`/`display`
- [ ] Registered in `cli/src/adaptors/registry.py`
- [ ] Auth header shape, API URL structure, pagination, and rate limits
      verified against the provider's current API docs (fetched and
      cited) — not assumed from another adaptor's pattern or from memory
- [ ] Path A: `parse_issue_url_info` resolves the ticket to a
      `(git_platform, repo)` pair via `RepositoryMapping`, not via local
      URL parsing; `fetch_issue_context`/`post_issue_comment` branch to a
      source-specific client; `provider_to_platform` audited at every
      call site rather than assumed
- [ ] Path B: workflow registered via `@register`; agents' `output_schema`
      fields have no `default=` (see `agents/base.py::_strict_schema`)
- [ ] Plugin/workflow emits `[JEANCLODE:RESULT]` / `[JEANCLODE:ERROR]`
      markers
- [ ] Backend: `Organization`/`Provider` enum extended, settings model
      registered, connection router, webhook router (if applicable),
      dispatch model matching the chosen path, config wired into
      `docker.yaml`/`kubernetes.yaml` (see add-backend.md)
- [ ] Sandbox/security-proxy upstreams declared (see sandbox-proxy.md)
- [ ] Frontend connect flow + settings UI wired (see add-frontend.md)
- [ ] `make generate-types` run after adding backend routers
- [ ] `make qa` passes
- [ ] `make test` passes
