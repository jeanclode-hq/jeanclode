"""Run — the main entry point. Workflow registry → RunContext → Workflow.run."""

import logging
import os
import sys
import time
from pathlib import Path

from claude_agent_sdk import ResultError

from src.adaptors import find_adaptor
from src.adaptors.base import Adaptor
from src.adaptors.github.auth import resolve_token as resolve_github_token
from src.adaptors.gitlab.auth import resolve_token as resolve_gitlab_token
from src.agents.utils import usage_dict
from src.cli import CLIArgs
from src.config import Config
from src.config_file import run_interactive_setup
from src.output import (
    ProgressTracker,
    emit_error,
    emit_rate_limit_error,
    emit_result,
    emit_step,
    extract_retry_after,
)
from src.runner.display_subscriber import UsageAccumulator, subscribe_display
from src.runner.preflight import (
    authenticate,
    build_env,
    clone_repos,
    fetch_context,
    load_config,
    resolve_sources,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.mcp_connectors import load_mcp_servers_from_env
from src.runtime.notify import load_notify_users_from_env
from src.skills.discovery import load_skills, load_skills_from_env
from src.skills.thirdparty import clone_thirdparty_plugins_from_env
from src.workflows import Workflow, find_workflow
from src.workspace import PLACEHOLDER_TOKENS, cleanup_workspace, create_workspace

logger = logging.getLogger(__name__)

# Only these workflows are safe to run from a bare terminal invocation.
# Everything else depends on the backend's async webhook loop (CI-gating,
# ready-notice, re-review) to finish what a single run starts, and would
# silently skip that verification run standalone — so a new workflow is
# container-only by default until it's added here. Container mode (the
# backend's own sandbox) always bypasses this check.
LOCAL_ALLOWED_WORKFLOWS = frozenset({"code-review", "pr-summary", "echo"})


async def run(args: CLIArgs) -> int:
    if args.command == "config":
        run_interactive_setup()
        return 0

    config = load_config(args)
    workflow_cls = find_workflow(
        url=args.urls[0].url if args.urls else None,
        command=args.adaptor_command,
    )
    if workflow_cls is None:
        target = args.adaptor_command or (args.urls[0].url if args.urls else "<empty>")
        msg = f"No workflow registered for: {target}"
        is_tty = sys.stderr.isatty() and not args.debug
        emit_error("no_workflow", msg, is_tty=is_tty)
        if is_tty:
            print(f"jeanclode: {msg}", file=sys.stderr)
        return 1

    container_mode = os.environ.get("JEANCLODE_CONTAINER_MODE") == "1"
    if workflow_cls.name not in LOCAL_ALLOWED_WORKFLOWS and not container_mode:
        msg = (
            f"'{workflow_cls.name}' is not available from the terminal — it needs the "
            "jeanclode backend for CI-gating and follow-up. Use code-review or "
            "pr-summary locally, or connect this repo through the platform for "
            "autonomous fixes."
        )
        is_tty = sys.stderr.isatty() and not args.debug
        emit_error("local_mode_unsupported", msg, is_tty=is_tty)
        if is_tty:
            print(f"jeanclode: {msg}", file=sys.stderr)
        return 1

    return await _run_workflow(args, config, workflow_cls)


async def _run_workflow(
    args: CLIArgs,
    config: Config,
    workflow_cls: type[Workflow],
) -> int:
    tracker = _make_tracker(args, config, skill=workflow_cls.name)
    is_tty = tracker is not None
    workspace = create_workspace()
    cwd = Path(workspace)
    env: dict[str, str] = {}
    related_repos: list[dict[str, str]] = []
    issue_urls: list[str] = [u.url for u in args.urls]

    adaptor = _adaptor_for_args(args)
    if adaptor is not None:
        try:
            cwd, env, related_repos = _preflight(
                adaptor, args, config, workspace, workflow_cls.name
            )
        except Exception as exc:
            emit_error("preflight", str(exc), is_tty=is_tty)
            cleanup_workspace(workspace)
            if tracker is not None:
                tracker.fail(str(exc))
                tracker.finish()
            return 1

    events = EventBus()
    usage_acc = UsageAccumulator()
    events.subscribe(usage_acc)
    subscribe_display(events, tracker)

    if tracker:
        tracker.step(f"Running {workflow_cls.name}...")
    elif not is_tty:
        emit_step("workflow", "started", detail=workflow_cls.name)

    t0 = time.time()
    try:
        skills = load_skills_from_env()
        thirdparty_roots = clone_thirdparty_plugins_from_env(workspace)
        if thirdparty_roots:
            skills = skills + load_skills(thirdparty_roots)
        if skills:
            logger.info(
                "loaded %d third-party skill(s): %s",
                len(skills),
                ", ".join(f"{s.name} ({s.plugin_path})" for s in skills),
            )
        mcp_servers = load_mcp_servers_from_env()
        if mcp_servers:
            logger.info("loaded %d org MCP server(s)", len(mcp_servers))
        # Set only when the workspace opted into memory — its presence is the on/off signal.
        memory_enabled = bool(os.environ.get("JEANCLODE_MEMORY_API_URL"))
        notify_users = load_notify_users_from_env()
        if notify_users:
            logger.info("notify list: %d user(s)", len(notify_users))
        ctx = RunContext(
            cwd=cwd,
            env=env,
            workspace=Path(workspace),
            model=config.get_model(),
            small_model=config.get_small_model(),
            events=events,
            skills=skills,
            mcp_servers=mcp_servers,
            issues=issue_urls,
            related_repos=related_repos,
            notify_users=notify_users,
            dry_run=args.dry_run,
            debug=args.debug,
            memory_enabled=memory_enabled,
        )
        result = await workflow_cls().run(ctx)

        if tracker:
            tracker.done(result.summary or result.status)
            tracker.summary(usage_acc.total)
        elif not is_tty:
            emit_step(
                "workflow",
                "completed",
                detail=workflow_cls.name,
                duration=time.time() - t0,
            )

        emit_result(
            {
                "status": result.status,
                "workflow": workflow_cls.name,
                "summary": result.summary,
                "data": result.data,
                "usage": usage_dict(usage_acc.total),
            },
            is_tty=is_tty,
        )
        return 0 if result.status == "success" else 1
    except ResultError as exc:
        # The SDK already resolves the most informative text it has (the
        # CLI's own errors[]/result/subtype/api_error_status, in that
        # priority order) into str(exc) — no need to re-derive it here.
        # Surface the few fields it *doesn't* fold into the message so a
        # transient API failure is distinguishable from a genuine agent
        # error without reading raw container logs.
        detail = str(exc)
        extras = [
            f"{label}={value}"
            for label, value in (
                ("http_status", exc.api_error_status),
                ("terminal_reason", exc.terminal_reason),
            )
            if value is not None
        ]
        if extras:
            detail = f"{detail} ({', '.join(extras)})"
        if exc.api_error_status == 429:
            if not is_tty:
                emit_rate_limit_error(detail, retry_after=extract_retry_after(exc))
        else:
            emit_error("claude_result_error", detail, is_tty=is_tty, usage=usage_acc.total)
        if tracker:
            tracker.fail(detail, usage_acc.total)
            return 1
        raise
    except Exception as exc:
        emit_error("unexpected", str(exc), is_tty=is_tty, usage=usage_acc.total)
        if tracker:
            tracker.fail(str(exc), usage_acc.total)
            return 1
        raise
    finally:
        cleanup_workspace(workspace)
        if tracker:
            tracker.finish()


def _adaptor_for_args(args: CLIArgs) -> Adaptor | None:
    """Pick an adaptor matching the first URL, if any.

    The workflow registry is the source of truth for *what* to run; the
    adaptor still owns *how* to authenticate, clone, and shape env vars.
    """
    if not args.urls:
        return None
    return find_adaptor(args.urls[0].url)


def _preflight(
    adaptor: Adaptor,
    args: CLIArgs,
    config: Config,
    workspace: Path,
    skill_name: str,
) -> tuple[Path, dict[str, str], list[dict[str, str]]]:
    """Authenticate, clone source, fetch context, and build the env dict.

    The adaptor's token authenticates the source platform (e.g. Sentry's
    issue API). When the resolved repo lives on a different forge, we need
    the forge's own credentials for the clone — without that, ``git clone``
    embeds the wrong token and gets rejected with "Invalid username or
    token". Pick the right one based on the resolved repo URL's host.

    After cloning, ``fetch_context`` populates ``.context/`` for
    adaptors that need it (github/gitlab PR review). Sentry's is a no-op.

    Returns the resolved cwd, the env dict, and any ``--related-repo``
    repos cloned alongside the primary (as ``{"name", "path"}`` dicts for
    injection into the agent's prompt — see ``BaseAgent.invoke``).
    """
    container_mode = os.environ.get("JEANCLODE_CONTAINER_MODE") == "1"
    token = authenticate(adaptor, container_mode=container_mode)
    sources = resolve_sources(adaptor, args, token, skill_name=skill_name)
    clone_token = _clone_token_for(sources, container_mode=container_mode) or token
    _, cwd, related = clone_repos(
        sources, args, clone_token, skill_name=skill_name, workspace=workspace
    )
    cwd_path = Path(cwd) if cwd else workspace
    fetch_context(adaptor, sources, str(cwd_path), token)
    env = build_env(config, adaptor, token, [u.url for u in args.urls])
    if container_mode and token in PLACEHOLDER_TOKENS:
        env.setdefault("JEANCLODE_CONTAINER_MODE", "1")
    related_repos = [{"name": Path(path).name, "path": path} for _url, path in related]
    return cwd_path, env, related_repos


def _clone_token_for(
    sources: dict[str, tuple[str, str | None, str | None]],
    *,
    container_mode: bool,
) -> str | None:
    """Resolve a credential matching the host of the first cloned repo.

    Falls back through env → ``gh auth token`` / ``glab auth token``.
    Returns ``None`` when no source is set or when no credential is
    available — callers then keep the platform token (or empty in
    container mode, where the security-proxy injects auth on the wire).
    """
    if container_mode or not sources:
        return None
    repo_url = next(iter(sources.values()))[0]
    if "github.com" in repo_url:
        return resolve_github_token()
    if "gitlab" in repo_url:
        return resolve_gitlab_token()
    return None


def _make_tracker(args: CLIArgs, config: Config, *, skill: str = "") -> ProgressTracker | None:
    """Create a progress tracker for TTY, non-debug mode."""
    if not sys.stderr.isatty() or args.debug:
        return None

    from src import __version__

    tracker = ProgressTracker(
        args.urls[0].url if len(args.urls) == 1 else f"{len(args.urls)} URLs",
        version=__version__,
        model=config.get_model(),
    )
    tracker.set_skill(skill)
    logging.getLogger().setLevel(logging.CRITICAL)
    tracker.start()
    return tracker
