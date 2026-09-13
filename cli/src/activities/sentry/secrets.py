"""Gitleaks helper — scan a string for secrets before posting it externally."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from src.activities.decorator import activity
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)


class SecretsDetected(RuntimeError):
    """Raised when gitleaks finds secrets in the scanned text."""


@activity(name="Scanning for secrets")
def gitleaks_scan(text: str, *, ctx: RunContext) -> None:  # noqa: ARG001
    """Scan ``text`` via gitleaks; raise ``SecretsDetected`` if any are found.

    No-op when ``gitleaks`` is not on ``$PATH`` — local dev shouldn't break
    just because the binary isn't installed. Production sandboxes ship it.
    """
    if not shutil.which("gitleaks"):
        return

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
        )
    except subprocess.TimeoutExpired:
        logger.warning("gitleaks timed out — proceeding without scan")
        return

    if proc.returncode == 1 and proc.stdout.strip():
        try:
            findings = json.loads(proc.stdout)
        except json.JSONDecodeError:
            findings = None
        if findings:
            msg = "secrets detected in scanned text"
            raise SecretsDetected(msg)
