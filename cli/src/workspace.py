"""Workspace management — temp directory and repo cloning for each run."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# A clone can hit a transient failure unrelated to the repo itself — e.g.
# the security-proxy sidecar restarting mid-run after being OOM-killed by
# an earlier, larger clone. Retry a few times with backoff before giving
# up on the repo.
_CLONE_MAX_ATTEMPTS = 3
_CLONE_RETRY_BACKOFF_MS = 500  # doubles after each failed attempt


def create_workspace() -> Path:
    """Create a temporary workspace directory for this run."""
    path = Path(tempfile.mkdtemp(prefix="jeanclode-"))
    logger.debug("Created workspace: %s", path)
    return path


def cleanup_workspace(path: Path) -> None:
    """Remove the workspace directory."""
    try:
        shutil.rmtree(path)
        logger.debug("Cleaned up workspace: %s", path)
    except Exception:
        logger.warning("Failed to clean up workspace: %s", path)


def _target_dir_name(repo_url: str) -> str:
    path = _repo_path(repo_url)
    return path.replace("/", "_") if path else repo_url.rstrip("/").rsplit("/", 1)[-1]


def _repo_path(repo_url: str) -> str:
    """Extract owner/repo (or group/project) from a repo URL."""
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts[3:]) if len(parts) > 3 else parts[-1]


def _extract_zipball(zip_path: Path, target: Path) -> None:
    """Extract a zipball whose top-level dir is a single owner-repo-sha folder."""
    extract_dir = target.parent / f".extract-{target.name}"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        entries = list(extract_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            entries[0].rename(target)
        else:
            extract_dir.rename(target)
            return
    finally:
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def download_zipball(repo_url: str, sha: str, workspace: Path, token: str) -> Path:
    """Download a repo snapshot at `sha` via `gh`/`glab` and extract it.

    Used for PR/MR workflows where the analyzer needs to read the source
    state at the PR head, not the default branch. Avoids git fetch/checkout
    complexity (works fine with shallow auth, no SSH keys needed).
    """
    target = workspace / _target_dir_name(repo_url)
    repo_path = _repo_path(repo_url)
    zip_path = workspace / f"{target.name}-{sha[:8]}.zip"

    logger.info("Downloading %s @ %s into %s", repo_url, sha[:8], target)

    try:
        if "github.com" in repo_url:
            env = {**os.environ, "GH_TOKEN": token}
            with zip_path.open("wb") as f:
                subprocess.run(
                    ["gh", "api", f"repos/{repo_path}/zipball/{sha}"],
                    stdout=f,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=240,
                    env=env,
                )
        else:
            host = repo_url.split("//", 1)[-1].split("/", 1)[0]
            env = {**os.environ, "GITLAB_TOKEN": token}
            if host != "gitlab.com":
                env["GITLAB_HOST"] = f"https://{host}"
            encoded = quote(repo_path, safe="")
            endpoint = f"projects/{encoded}/repository/archive.zip?sha={sha}"
            with zip_path.open("wb") as f:
                subprocess.run(
                    ["glab", "api", endpoint],
                    stdout=f,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=240,
                    env=env,
                )

        _extract_zipball(zip_path, target)
    finally:
        zip_path.unlink(missing_ok=True)

    return target


_SANDBOX_PLACEHOLDER = "sandbox-placeholder"

# Every stand-in the backend or this CLI puts where a real credential would
# go in container mode. They all have to be recognised as "not a credential":
# ``GITLAB_TOKEN=proxy-injected`` exists purely so ``glab`` will make the
# request at all, and embedding it in a clone URL sends the literal string as
# the git password — which GitLab reports as "HTTP Basic: Access denied",
# indistinguishable from a bad token.
PLACEHOLDER_TOKENS = frozenset({_SANDBOX_PLACEHOLDER, "sandbox-proxy-injected", "proxy-injected"})

_FALLBACK_GIT_NAME = "JeanClode"
_FALLBACK_GIT_EMAIL = "jeanclode-bot@users.noreply.github.com"


def ensure_local_git_identity(repo_dir: Path) -> bool:
    """Write a local git identity when none is configured. No-op otherwise."""
    proc = subprocess.run(
        ["git", "config", "user.email"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return False
    subprocess.run(
        ["git", "config", "user.email", _FALLBACK_GIT_EMAIL],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", _FALLBACK_GIT_NAME],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    return True


def disable_commit_signing(repo_dir: Path) -> None:
    """Force ``commit.gpgsign=false`` locally, regardless of the host's global config.

    Bot commits (our own placeholder commit in create_worktree, and whatever
    the fixer agent commits later via its own Bash tool in this same repo)
    must never attempt to sign with the operator's personal GPG key — and on
    a dev machine with ``commit.gpgsign=true`` globally, signing would also
    block on an interactive pinentry prompt that doesn't exist in this
    non-interactive process, hanging or failing the run. Local config is
    shared by every worktree of this repo (no ``extensions.worktreeConfig``),
    so setting it once here covers worktrees too.
    """
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def _embed_token(repo_url: str, token: str) -> str:
    """Embed an HTTPS auth pair into github/gitlab URLs for local-mode clones.

    Plain ``git clone`` reads no auth from env — it relies on a credential
    helper. In sandbox mode the security-proxy injects auth on the wire so
    no embedding is needed (and no placeholder is a real credential
    anyway). In local mode the token comes from env / ``gh auth token`` and
    we embed it so private repos work without configuring a helper.
    """
    if not token or token in PLACEHOLDER_TOKENS or not repo_url.startswith("https://"):
        return repo_url
    host = repo_url.split("//", 1)[1].split("/", 1)[0].lower()
    if host == "github.com" or host.endswith(".github.com"):
        user = "x-access-token"
    elif host == "gitlab.com" or "gitlab" in host:
        user = "oauth2"
    else:
        return repo_url
    return repo_url.replace("https://", f"https://{user}:{token}@", 1)


def clone_repo(
    repo_url: str,
    workspace: Path,
    token: str = "",
    *,
    ref: str | None = None,
) -> Path:
    """Shallow-clone a repo into the workspace. Returns the workdir path.

    When ``ref`` is given (PR/MR head branch), clones that branch directly
    via ``--branch <ref>`` so the working tree is on the right branch with
    upstream tracking already set — required for write-needing workflows
    (respond, fix) that will commit and ``git push origin HEAD``.
    """
    target = workspace / _target_dir_name(repo_url)
    clone_url = _embed_token(repo_url, token)

    logger.info("Cloning %s%s into %s", repo_url, f" @ {ref}" if ref else "", target)

    clone_env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    cmd = [
        "git",
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
    ]
    if ref:
        cmd += ["--branch", ref]
    cmd += [clone_url, str(target)]

    backoff_ms = _CLONE_RETRY_BACKOFF_MS
    for attempt in range(1, _CLONE_MAX_ATTEMPTS + 1):
        shutil.rmtree(target, ignore_errors=True)
        proc = subprocess.run(cmd, capture_output=True, env=clone_env, text=True)
        if proc.returncode == 0:
            break
        if attempt == _CLONE_MAX_ATTEMPTS:
            # Strip embedded token from any error output before surfacing it.
            safe_url = repo_url
            stderr = (
                proc.stderr.strip().replace(clone_url, safe_url) if token else proc.stderr.strip()
            )
            stdout = (
                proc.stdout.strip().replace(clone_url, safe_url) if token else proc.stdout.strip()
            )
            msg = (
                f"clone failed for {safe_url} (exit={proc.returncode}, "
                f"{attempt} attempts)\n"
                f"--- stderr ---\n{stderr}\n"
                f"--- stdout ---\n{stdout}"
            )
            raise RuntimeError(msg)
        logger.warning(
            "Clone attempt %d/%d failed for %s (exit=%d), retrying in %dms",
            attempt,
            _CLONE_MAX_ATTEMPTS,
            repo_url,
            proc.returncode,
            backoff_ms,
        )
        time.sleep(backoff_ms / 1000)
        backoff_ms *= 2

    ensure_local_git_identity(target)
    disable_commit_signing(target)
    return target
