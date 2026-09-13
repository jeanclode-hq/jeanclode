# jeanclode

The CLI runner for [Jeanclode](https://github.com/jeanclode-hq/jeanclode) — a
self-hosted autonomous coding agent. A URL (or an explicit subcommand) selects a
workflow; the workflow orchestrates deterministic activities and LLM-backed
agents through the Claude Agent SDK.

Run from a bare terminal it renders a Rich progress UI. As a container entrypoint
it emits structured JSON on stdout for the backend to consume.

## Install

```bash
pip install jeanclode
# or
uv tool install jeanclode
```

Requires Python 3.14+ and a working [Claude Code](https://claude.com/claude-code)
setup (`claude` on `PATH`, authenticated).

## Usage

```bash
# Code review on a GitHub PR (default for a PR URL)
jeanclode https://github.com/org/repo/pull/123

# PR summary instead of review
jeanclode summary https://github.com/org/repo/pull/123

# GitLab MR review (gitlab.com or self-hosted)
jeanclode https://gitlab.com/group/project/-/merge_requests/42
jeanclode review https://gitlab.example.com/group/project/-/merge_requests/42

# Interactive setup (tokens, model, defaults)
jeanclode config
```

Flags: `--repo`, `--related-repo` (repeatable), `--dry-run`, `--debug`,
`--model`.

`code-review`, `pr-summary` and `echo` are the workflows that run from a bare
terminal invocation. The rest — `sentry-fix`, `issue-resolve`,
`jeanclode-respond` — run only as the container entrypoint, since their CI-gate,
labeling and review loop depend on the backend's webhook loop.

## License

AGPL-3.0
