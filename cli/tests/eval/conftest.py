"""Shared fixtures for LLM eval tests."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from deepeval import assert_test
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase

from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import Event, ToolCall, ToolResult
from tests.eval.judge import GeminiJudge, get_judge

pytestmark = pytest.mark.eval

FAKE_GLAB_SCRIPT = Path(__file__).parent / "fixtures/fake_glab.sh"
FAKE_GH_SCRIPT = Path(__file__).parent / "fixtures/fake_gh.sh"


@pytest.fixture(scope="session")
def judge() -> GeminiJudge:
    return get_judge()


@pytest.fixture
def run_agent_result(
    tmp_path: Path,
) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
    """Return a coroutine factory that runs an agent with a minimal RunContext.

    Returns the full ``AgentResult`` (``.text`` + ``.structured``). Use this
    directly instead of ``run_agent`` for agents with no ``output_schema``
    (e.g. ``SynthesizerAgent``), where ``.structured`` is always ``None`` and
    the real output has to be parsed out of ``.text``.

    Prints a readable trace (tool calls/results, then final text) to stdout.
    pytest only surfaces captured stdout for *failing* tests, so this stays
    silent on green runs but shows exactly what the agent did the moment an
    assertion fails -- no need to rerun with ``-s`` to see what happened.
    """

    async def _run(agent_cls: Any, agent_input: Any, **ctx_fields: Any) -> Any:
        bus = EventBus()
        trace: list[str] = []

        def _record(event: Event) -> None:
            if isinstance(event, ToolCall):
                trace.append(f"-> {event.name}: {event.summary or event.input}")
            elif isinstance(event, ToolResult):
                trace.append(f"<- {event.name}: {event.output[:800]}")

        bus.subscribe(_record)
        ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=bus, **ctx_fields)
        result = await agent_cls().invoke(agent_input, ctx)
        trace.append(f"=== final text ===\n{result.text}")
        print("\n".join(trace))
        return result

    return _run


@pytest.fixture
def run_agent(
    run_agent_result: Callable[[Any, Any], Coroutine[Any, Any, Any]],
) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
    """Return a coroutine factory that runs an agent and returns ``.structured``."""

    async def _run(agent_cls: Any, agent_input: Any, **ctx_fields: Any) -> Any:
        result = await run_agent_result(agent_cls, agent_input, **ctx_fields)
        return result.structured

    return _run


@pytest.fixture
def fake_cli_env(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Inject arg-aware fake glab + gh into PATH for one test.

    Use via indirect parametrize::

        @pytest.mark.parametrize("fake_cli_env", [{
            "glab": {"mr_list_org_repo_42.json": [...]},
            "gh":   {"issue_org_repo_7.json": {...}},
        }], indirect=True)

    Yields a dict with:
      - ``log``: Path to the call log (use with :func:`assert_cli_called`)
      - ``path_prepend``: str to prepend to PATH so agents pick up the fake CLIs
    """
    fixtures_dir = tmp_path / "cli_fixtures"
    fixtures_dir.mkdir()
    log_file = tmp_path / "cli_calls.log"

    param: dict[str, Any] = getattr(request, "param", {}) or {}
    for name, content in param.get("glab", {}).items():
        (fixtures_dir / name).write_text(json.dumps(content))
    for name, content in param.get("gh", {}).items():
        (fixtures_dir / name).write_text(json.dumps(content))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for script_src, name in [(FAKE_GLAB_SCRIPT, "glab"), (FAKE_GH_SCRIPT, "gh")]:
        dest = bin_dir / name
        dest.write_text(
            script_src.read_text()
            .replace("__LOG__", str(log_file))
            .replace("__FIXTURES__", str(fixtures_dir))
        )
        dest.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return {"log": log_file, "path_prepend": str(bin_dir)}


async def aassert_test(test_case: LLMTestCase, metrics: list[BaseMetric]) -> None:
    """Async-safe wrapper around DeepEval's assert_test.

    assert_test calls loop.run_until_complete() via nest_asyncio, which corrupts
    the pytest-asyncio event loop for subsequent tests. Running it in a thread
    gives it an isolated event loop so the test loop stays clean.

    After assert_test returns we cancel litellm's background LoggingWorker tasks
    and close the thread's event loop, which suppresses the
    "Task was destroyed but it is pending!" warnings from asyncio.
    """

    def _run() -> None:
        try:
            assert_test(test_case, metrics)
        finally:
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()
            except RuntimeError:
                pass

    await asyncio.to_thread(_run)


def assert_cli_called(log_file: Path, *fragments: str) -> None:
    """Assert at least one logged call contains all fragments.

    Example::

        assert_cli_called(env["log"], "mr list", "-R org/repo", "--search 42")
        assert_cli_called(env["log"], "issue update", "7", "--label", "jeanclode:resolve")
    """
    calls = log_file.read_text().splitlines() if log_file.exists() else []
    match = any(all(f in call for f in fragments) for call in calls)
    assert match, f"No call matched {fragments!r}.\nActual calls:\n" + "\n".join(calls)
