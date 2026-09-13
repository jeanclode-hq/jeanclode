"""gitleaks_scan — best-effort secret detection on a string."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.activities.sentry import gitleaks_scan
from src.activities.sentry.secrets import SecretsDetected
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@patch("src.activities.sentry.secrets.shutil.which", return_value=None)
def test_no_op_when_gitleaks_missing(_which: Any, ctx: RunContext) -> None:
    gitleaks_scan("AKIA1234", ctx=ctx)


@patch("src.activities.sentry.secrets.shutil.which", return_value="/usr/bin/gitleaks")
@patch("src.activities.sentry.secrets.subprocess.run")
def test_passes_when_no_findings(run: Any, _which: Any, ctx: RunContext) -> None:
    run.return_value = _proc("", returncode=0)
    gitleaks_scan("hello world", ctx=ctx)


@patch("src.activities.sentry.secrets.shutil.which", return_value="/usr/bin/gitleaks")
@patch("src.activities.sentry.secrets.subprocess.run")
def test_raises_when_findings_present(run: Any, _which: Any, ctx: RunContext) -> None:
    run.return_value = _proc(json.dumps([{"RuleID": "aws-key"}]), returncode=1)
    with pytest.raises(SecretsDetected):
        gitleaks_scan("AKIASECRET", ctx=ctx)


@patch("src.activities.sentry.secrets.shutil.which", return_value="/usr/bin/gitleaks")
@patch("src.activities.sentry.secrets.subprocess.run")
def test_passes_when_timeout(run: Any, _which: Any, ctx: RunContext) -> None:
    run.side_effect = subprocess.TimeoutExpired(cmd="gitleaks", timeout=10)
    gitleaks_scan("body", ctx=ctx)
