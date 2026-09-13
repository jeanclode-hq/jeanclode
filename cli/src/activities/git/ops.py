"""Git repo and worktree operations — clone, worktree, push."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from src.activities.decorator import activity
from src.activities.git.schemas import WorktreePath
from src.runtime.context import RunContext
from src.workspace import PLACEHOLDER_TOKENS, _target_dir_name, clone_repo

logger = logging.getLogger(__name__)


def default_branch(cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        ref = proc.stdout.strip()
        return ref.removeprefix("refs/remotes/origin/") if ref else "main"
    except subprocess.CalledProcessError:
        return "main"


# When the agent runs sandboxed, no real credential is in ctx.env — the
# security-proxy sidecar strips Authorization and injects the real value
# from its own config. Treating every placeholder as "no token" keeps us
# from embedding garbage into clone URLs. See ``PLACEHOLDER_TOKENS``.


def _resolve_clone_token(repo_url: str, ctx: RunContext) -> str:
    if ctx.env.get("JEANCLODE_CONTAINER_MODE") == "1":
        return ""

    def _real(value: str | None) -> str | None:
        if value and value not in PLACEHOLDER_TOKENS:
            return value
        return None

    if "github.com" in repo_url:
        env_token = _real(ctx.env.get("GH_TOKEN")) or _real(ctx.env.get("GITHUB_TOKEN"))
        if env_token:
            return env_token
        from src.adaptors.github.auth import resolve_token

        return resolve_token() or ""
    if "gitlab" in repo_url:
        env_token = _real(ctx.env.get("GITLAB_TOKEN"))
        if env_token:
            return env_token
        from src.adaptors.gitlab.auth import resolve_token

        return resolve_token() or ""
    return ""


@activity(name="Cloning repository")
def ensure_repo(repo_url: str, *, ctx: RunContext) -> Path:
    """Return a path to a cloned repo_url under ctx.workspace.

    Re-uses an existing clone if one is already present, otherwise does a
    shallow clone with a host-appropriate token.
    """
    target = ctx.workspace / _target_dir_name(repo_url)
    if (target / ".git").exists():
        return target
    token = _resolve_clone_token(repo_url, ctx)
    return clone_repo(repo_url, ctx.workspace, token)


@activity(name="Setting up worktree")
def create_worktree(branch: str, *, ctx: RunContext, dest: Path | None = None) -> WorktreePath:
    """Add a fresh git worktree on a new branch off the default branch.

    Also makes a single empty commit so the branch has a tip distinct from
    the default branch — required by gh pr create / glab mr create, which
    refuse to open a PR when the branch is identical to the base. Its SHA
    is returned so callers can later tell "nothing committed yet" apart
    from a real fix (see fix_missing_reason).

    ``dest`` overrides where the worktree is created — issue-resolve's
    multi-repo fixes call this once per target repo and need each one at
    a distinct path (e.g. siblings under one parent) rather than the
    default single-worktree-per-branch layout every other caller uses.
    """
    base = default_branch(ctx.cwd)
    wt_path = dest if dest is not None else ctx.workspace / "worktrees" / branch.replace("/", "_")
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt_path), base],
        cwd=ctx.cwd,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = f"git worktree add failed (exit={proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", f"chore: open draft for {branch}"],
        cwd=wt_path,
        check=True,
        capture_output=True,
    )
    placeholder_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return WorktreePath(path=wt_path, branch=branch, placeholder_sha=placeholder_sha)


def fix_missing_reason(cwd: Path, branch: str, placeholder_sha: str) -> str | None:
    """Return a corrective message if ``branch`` has no real, pushed fix yet.

    Fixer agents commit and push their own work via the Bash tool (see the
    fixer prompts) rather than through a runner activity, so this is the
    only place that verifies they actually did. Checked from two call
    sites: a Stop hook that nudges the agent to self-correct mid-turn
    (src/agents/hooks.py), and a post-invoke guard in the workflow runner
    that refuses to mark the PR ready / report success otherwise.

    Returns None when a real, pushed commit exists; otherwise a
    human-readable reason suitable for feeding back to the agent. Only
    HEAD and the remote are checked — a dirty working tree (e.g. bytecode
    cache from the fixer's own static-check step) isn't: a pushed commit is
    the signal that matters, not local cleanliness afterward.

    Compares against the remote directly via ``git ls-remote`` rather than
    ``git fetch`` + ``rev-parse origin/<branch>``: repos here are cloned
    with ``--single-branch`` (see ensure_repo), which restricts
    ``remote.origin.fetch`` to the default branch only. Fetching any other
    branch by name still succeeds (updates FETCH_HEAD) but never creates
    or updates its ``refs/remotes/origin/<branch>`` — so comparing against
    that ref would report "not pushed" unconditionally, even for a commit
    that's genuinely on the remote.

    Raises RuntimeError if ``git ls-remote`` itself fails outright
    (network/proxy issue) — that's an infra problem, not a "didn't push"
    problem, and callers must not conflate the two.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()
    if not head or head == placeholder_sha:
        return (
            "No code changes have been committed yet — the branch is still just "
            "the empty placeholder commit. Implement the fix described in the "
            "plan, then commit and push before finishing."
        )

    ls_remote = subprocess.run(
        ["git", "ls-remote", "origin", branch], cwd=cwd, capture_output=True, text=True
    )
    if ls_remote.returncode != 0:
        msg = f"Could not verify whether the push succeeded: `git ls-remote origin {branch}` failed: {ls_remote.stderr.strip()}"
        raise RuntimeError(msg)

    remote_sha = ls_remote.stdout.split()[0] if ls_remote.stdout.strip() else None
    if remote_sha != head:
        return f"Your commits haven't been pushed yet. Run `git push origin {branch}` before finishing."

    return None


@activity(name="Verifying fix was pushed")
def verify_fix_pushed(branch: str, placeholder_sha: str, *, ctx: RunContext) -> str | None:
    """Runner-side guard: see fix_missing_reason for what this checks."""
    return fix_missing_reason(ctx.cwd, branch, placeholder_sha)


@activity(name="Pushing branch")
def push_branch(branch: str, *, ctx: RunContext) -> None:
    """Push the current branch to origin with upstream tracking.

    Force-pushes: ``branch`` is a deterministic, bot-owned name (see
    issue_branch / branch_name), so a retried execution reuses it and must
    be able to overwrite a stale attempt from a prior failed run rather
    than fail on a non-fast-forward push.
    """
    subprocess.run(
        ["git", "push", "-f", "-u", "origin", branch],
        cwd=ctx.cwd,
        check=True,
        capture_output=True,
    )
