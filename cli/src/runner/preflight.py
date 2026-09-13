"""Pre-flight — everything that happens before the skill runs.

Config loading, adaptor detection, auth, repo resolution, cloning, env building.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from src.adaptors import find_adaptor
from src.adaptors.base import Adaptor
from src.cli import CLIArgs
from src.config import Config
from src.config_file import load_config_file, run_interactive_setup
from src.workspace import clone_repo, create_workspace, download_zipball

logger = logging.getLogger(__name__)


def load_config(args: CLIArgs) -> Config:
    """Load config from env, file, or interactive setup."""
    file_config = load_config_file()
    has_model = args.model is not None or bool(os.environ.get("JEANCLODE_MODEL"))
    if file_config is None and not has_model:
        import sys

        if sys.stdin.isatty():
            file_config = run_interactive_setup()

    config = Config.from_env(model=args.model, file_config=file_config)
    if args.debug:
        config = config.model_copy(update={"debug": True})
    return config


def detect_adaptor(url: str, *, command: str | None = None) -> tuple[Adaptor, str]:
    """Resolve (adaptor, skill_name) for a URL + optional CLI subcommand.

    URL match selects the adaptor. When the user gave an explicit
    subcommand we honor it; otherwise we let the adaptor inspect the URL
    via ``select_command`` (e.g. github/gitlab return ``"respond"`` for
    URLs with comment fragments).
    """
    adaptor = find_adaptor(url)
    if not adaptor:
        msg = f"Could not detect what to do with: {url}"
        raise ValueError(msg)
    cmd = command or adaptor.select_command(url)
    if cmd not in adaptor.skills:
        supported = ", ".join(adaptor.skills.keys())
        msg = f"Adaptor '{adaptor.name}' does not support command '{cmd}' (supports: {supported})"
        raise ValueError(msg)
    return adaptor, adaptor.skills[cmd]


SANDBOX_PLACEHOLDER_TOKEN = "sandbox-placeholder"


def authenticate(adaptor: Adaptor, *, container_mode: bool = False) -> str:
    """Resolve auth token. In container mode the security-proxy sidecar
    injects real credentials on the wire, so the agent runs token-less.
    We return a placeholder string (rather than empty) because tools like
    ``gh``/``glab`` refuse to send a request without a non-empty token —
    the proxy strips and rewrites the Authorization header regardless."""
    token = adaptor.resolve_auth()
    if token:
        return token
    if container_mode:
        return SANDBOX_PLACEHOLDER_TOKEN
    msg = f"No auth token found for {adaptor.name}. Check your environment variables."
    raise ValueError(msg)


# Skills that only read the source — fetched as a zipball at the head SHA
# (faster, no .git overhead). Anything not in this set needs a real git
# tree because the workflow commits and pushes (respond, sentry-fix).
_READ_ONLY_SKILLS = frozenset({"code-review", "pr-summary"})


def resolve_sources(
    adaptor: Adaptor, args: CLIArgs, token: str, *, skill_name: str = ""
) -> dict[str, tuple[str, str | None, str | None]]:
    """Map URLs to (repo_url, sha, head_ref) tuples.

    ``sha`` is the source state the workflow needs; None means "default branch".
    ``head_ref`` is the PR/MR head branch name, populated only when ``skill_name``
    is a write-needing workflow — read-only skills don't need it (they fetch
    a zipball at ``sha``) so we skip the extra API call.
    """
    needs_ref = bool(skill_name) and skill_name not in _READ_ONLY_SKILLS
    result: dict[str, tuple[str, str | None, str | None]] = {}
    for entry in args.urls:
        repo_url = adaptor.resolve_repo_url(entry.url, token, repo_override=entry.repo)
        if repo_url:
            sha = adaptor.fetch_sha(entry.url, token)
            head_ref = adaptor.fetch_head_ref(entry.url, token) if needs_ref and sha else None
            result[entry.url] = (repo_url, sha, head_ref)
    return result


def clone_repos(
    sources: dict[str, tuple[str, str | None, str | None]],
    args: CLIArgs,
    token: str,
    *,
    skill_name: str = "",
    workspace: Path | None = None,
) -> tuple[Path, str, list[tuple[str, str]]]:
    """Fetch source for each URL into a workspace. Returns (workspace, cwd, related).

    Pass ``workspace`` to reuse an existing temp dir — without it we'd
    create a second one, and the workflow would end up with ``ctx.cwd``
    in one tree and ``ctx.workspace`` in another.

    Routing per (skill, sha):
    - read-only skill (code-review / pr-summary) + sha → zipball at sha
      (fastest, smallest, no .git needed since the workflow only reads).
    - any write-needing skill (respond, sentry-fix) → shallow ``git clone``.
      When a head ref is known we clone that branch directly so HEAD is
      on the right branch with upstream tracking — required for the
      ``push-and-retrigger.sh`` flow.
    - no sha → shallow clone of the default branch (sentry-fix path).

    Same (repo_url, sha) is fetched once per workspace.

    ``related`` is the list of ``(repo_url, local_path)`` for any
    ``--related-repo`` URLs — repos grouped with the primary one (see
    ``RepositoryMapping`` on the backend) that get cloned alongside it into
    the same workspace so the agent can read across the group. These are
    always cloned at their default branch (no PR/MR head ref concept
    applies to them) and a failure is non-fatal — the primary workflow
    proceeds without that extra context rather than failing the whole run.
    """
    if workspace is None:
        workspace = create_workspace()
    cwd = str(workspace)

    if not sources:
        return workspace, cwd, []

    use_zipball = skill_name in _READ_ONLY_SKILLS
    fetched: dict[tuple[str, str | None], str] = {}
    failures: list[tuple[str, str]] = []
    for repo_url, sha, head_ref in {(r, s, h) for r, s, h in sources.values()}:
        try:
            if use_zipball and sha:
                workdir = download_zipball(repo_url, sha, workspace, token)
            else:
                workdir = clone_repo(repo_url, workspace, token, ref=head_ref)
            fetched[(repo_url, sha)] = str(workdir)
        except Exception as exc:
            logger.warning(
                "Failed to fetch source %s @ %s", repo_url, sha or "default", exc_info=True
            )
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            detail = (stderr or "").strip() or str(exc)
            failures.append((repo_url, detail))

    if not fetched:
        details = "; ".join(f"{url}: {err}" for url, err in failures) or "no sources fetched"
        raise RuntimeError(f"Failed to fetch any source: {details}")

    for entry in args.urls:
        spec = sources.get(entry.url)
        if spec is None:
            continue
        key = (spec[0], spec[1])
        if key in fetched:
            cwd = fetched[key]
            break

    seen_repo_urls = {repo_url for repo_url, _sha in fetched}
    related: list[tuple[str, str]] = []
    for entry in args.urls:
        for repo_url in entry.related_repos:
            if repo_url in seen_repo_urls:
                continue
            seen_repo_urls.add(repo_url)
            try:
                workdir = clone_repo(repo_url, workspace, token)
                related.append((repo_url, str(workdir)))
            except Exception:
                logger.warning("Failed to clone related repo %s", repo_url, exc_info=True)

    logger.info("Workspace: %s", cwd)
    logger.info("Related repos: %s", ", ".join(f"{r} -> {p}" for r, p in related))
    return workspace, cwd, related


def fetch_context(
    adaptor: Adaptor,
    sources: dict[str, tuple[str, str | None, str | None]],
    cwd: str,
    token: str,
) -> None:
    """Populate <cwd>/.context/ via the adaptor (no-op for sentry).

    Cache lives INSIDE the cloned repo so subagents can use the simple
    relative path `.context/<file>` — avoids Claude misexpanding `..`.

    Runs AFTER source has been cloned/zipballed and BEFORE the workflow
    starts, so cache files exist when agents read them.
    """
    if not sources:
        return
    first_url = next(iter(sources))
    try:
        adaptor.fetch_context(Path(cwd), first_url, token)
    except Exception:
        logger.warning("Failed to fetch context for %s", first_url, exc_info=True)


def build_env(
    config: Config, adaptor: Adaptor, token: str, issue_urls: list[str]
) -> dict[str, str]:
    """Build the full env dict for the SDK session."""
    env: dict[str, str] = {}

    if config.claude_code:
        if config.claude_code.oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = config.claude_code.oauth_token
        if config.claude_code.api_key:
            env["ANTHROPIC_API_KEY"] = config.claude_code.api_key

    if config.git_forge:
        if config.git_forge.gh_token:
            env["GH_TOKEN"] = config.git_forge.gh_token
        if config.git_forge.gitlab_token:
            env["GITLAB_TOKEN"] = config.git_forge.gitlab_token
        if config.git_forge.gitlab_host:
            env["GITLAB_HOST"] = config.git_forge.gitlab_host

    env.update(adaptor.build_env(token, issue_urls))
    return env
