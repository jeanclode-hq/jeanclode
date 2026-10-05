"""Stop whatever a demo round left running.

The demo agent starts its dev server and mocks in the background, and a round
that crashes or runs out of time never gets to stop them: they'd hold the ports
and memory of the next round, and keep writing into the checkout after its
setup was stashed. The fixer is parked on the hook while a round runs, so any
process born during the round is the round's.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PROC = Path("/proc")
_TERM_GRACE_S = 3.0


def running_pids() -> set[int]:
    """This user's processes. Empty outside a container, where anything new isn't ours."""
    if not os.environ.get("JEANCLODE_CONTAINER_MODE"):
        return set()
    uid = os.getuid()
    if _PROC.is_dir():
        pids = set()
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == uid:
                    pids.add(int(entry.name))
            except OSError:
                continue
        return pids
    proc = subprocess.run(
        ["ps", "-U", str(uid), "-o", "pid="], capture_output=True, text=True, check=False
    )
    return {int(pid) for pid in proc.stdout.split() if pid.isdigit()}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pids: set[int], sig: signal.Signals) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def stop_new_processes(before: set[int]) -> list[int]:
    """SIGTERM every process started since ``before``, then SIGKILL the holdouts."""
    if not before:
        return []
    new = running_pids() - before - {os.getpid()}
    if not new:
        return []
    _signal(new, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline and any(_alive(pid) for pid in new):
        time.sleep(0.1)
    _signal({pid for pid in new if _alive(pid)}, signal.SIGKILL)
    logger.info("stopped %d process(es) the demo round left running", len(new))
    return sorted(new)
