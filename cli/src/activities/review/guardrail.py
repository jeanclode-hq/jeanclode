from __future__ import annotations

import json
import shutil
import subprocess

from src.activities.decorator import activity
from src.activities.review.schemas import Comment
from src.runtime.context import RunContext


def _scan(text: str) -> bool:
    if not shutil.which("gitleaks"):
        return False
    try:
        proc = subprocess.run(
            [
                "gitleaks",
                "stdin",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                "-",
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if proc.returncode == 1 and proc.stdout.strip():
        try:
            return bool(json.loads(proc.stdout))
        except json.JSONDecodeError:
            return False
    return False


@activity(name="Scanning for secrets")
def apply_guardrail(comments: list[Comment], *, ctx: RunContext) -> list[Comment]:  # noqa: ARG001
    return [c for c in comments if not _scan(c.body)]
