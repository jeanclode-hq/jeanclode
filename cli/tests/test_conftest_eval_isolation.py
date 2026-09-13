"""Regression test for the eval-collection isolation guard in conftest.py.

Importing ``tests/eval`` (via ``deepeval``) calls ``load_dotenv()``, which
leaks the repo-root ``.env`` (real secrets like ``CLAUDE_CODE_OAUTH_TOKEN``)
into the process env for every other test in the same run. ``-m not eval``
only deselects those tests after collection, so it doesn't help —
``pytest_ignore_collect`` in ``tests/conftest.py`` has to stop collection
before ``tests/eval``'s modules are ever imported.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.conftest import pytest_ignore_collect


def _config(args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(invocation_params=SimpleNamespace(args=args))


def test_eval_dir_ignored_by_default() -> None:
    path = Path("/repo/cli/tests/eval/agents/test_triage_agent.py")
    assert pytest_ignore_collect(path, _config(["tests/"])) is True


def test_eval_dir_collected_when_explicitly_requested() -> None:
    path = Path("/repo/cli/tests/eval/agents/test_triage_agent.py")
    assert pytest_ignore_collect(path, _config(["tests/eval/", "-m", "eval"])) is False


def test_non_eval_path_untouched() -> None:
    path = Path("/repo/cli/tests/test_config.py")
    assert pytest_ignore_collect(path, _config(["tests/"])) is None
