"""Keep the demo's setup out of the fixer's checkout between rounds.

The demo agent edits config, mocks and fixtures in the same checkout the fixer
pushes from. Each round its edits go into a stash (never pushed) and come back
for the next round, so the fixer always sees a clean tree.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Outside every repo and the agents' working folder, so nothing here gets committed.
DEMO_DIR = Path("/tmp/jeanclode-demo")
HINTS_FILE = "hints.md"
BYPASS_FILE = "bypass"
# The demo's own setup outside the checkouts: shown under the demo, kept across rounds.
SETUP_DIR = "setup"
STASH_MESSAGE = "jeanclode-demo"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def git_status(cwd: Path) -> str:
    """``git status --porcelain``: empty on a clean checkout."""
    return _git(cwd, "status", "--porcelain").stdout


def is_clean(cwd: Path) -> bool:
    proc = _git(cwd, "status", "--porcelain")
    return proc.returncode == 0 and not proc.stdout.strip()


def head_sha(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _demo_stash(cwd: Path) -> str | None:
    listing = _git(cwd, "stash", "list", "--format=%gd%x00%s").stdout
    for line in listing.splitlines():
        ref, _, subject = line.partition("\x00")
        if subject.endswith(f": {STASH_MESSAGE}"):
            return ref
    return None


def restore_setup(cwd: Path) -> None:
    """Pop the previous round's setup back into a clean checkout.

    A pop that conflicts with what the fixer committed since is dropped: the
    checkout was clean before, so resetting it loses nothing of the fixer's,
    and the demo agent rebuilds its setup from memory and hints.
    """
    ref = _demo_stash(cwd)
    if ref is None:
        return
    if _git(cwd, "stash", "pop", ref).returncode == 0:
        return
    logger.warning("demo setup no longer applies in %s; starting the next round from scratch", cwd)
    _git(cwd, "reset", "--hard", "HEAD")
    _git(cwd, "clean", "-fd")
    _git(cwd, "stash", "drop", ref)


def stash_setup(cwd: Path) -> None:
    """Stash everything the demo left in ``cwd``."""
    stale = _demo_stash(cwd)
    if stale is not None:
        _git(cwd, "stash", "drop", stale)
    if is_clean(cwd):
        return
    if _git(cwd, "stash", "push", "--include-untracked", "-m", STASH_MESSAGE).returncode != 0:
        logger.warning("could not stash the demo setup in %s; discarding it", cwd)
        _git(cwd, "reset", "--hard", "HEAD")
        _git(cwd, "clean", "-fd")


def fresh_demo_dir(demo_dir: Path) -> None:
    """Start a run with no hints or bypass left over from an earlier one."""
    shutil.rmtree(demo_dir, ignore_errors=True)
    demo_dir.mkdir(parents=True, exist_ok=True)


def reset_demo_dir(demo_dir: Path) -> None:
    """Clear the last round's recording, keeping the fixer's hints and bypass and the setup."""
    demo_dir.mkdir(parents=True, exist_ok=True)
    for entry in demo_dir.iterdir():
        if entry.name in (HINTS_FILE, BYPASS_FILE, SETUP_DIR):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
