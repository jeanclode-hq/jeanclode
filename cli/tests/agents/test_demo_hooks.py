"""The demo gate, its wait on the CI gates, and the demo agent's publish guard."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import HookMatcher

from src.activities.demo import BYPASS_FILE, DemoGateState, DemoRound
from src.activities.git.schemas import WorktreePath
from src.agents.hooks import (
    DEMO_AGENT_TIMEOUT_S,
    MAX_DEMO_ROUNDS,
    GateResults,
    forbid_publishing_hook,
    require_demo_hook,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _so_input(tool_use_id: str = "tu_1") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "StructuredOutput",
        "tool_input": {},
        "tool_use_id": tool_use_id,
    }


def _decision(result: dict[str, Any]) -> str:
    return result.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _reason(result: dict[str, Any]) -> str:
    return result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def worktree(tmp_path: Path) -> WorktreePath:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.js").write_text("console.log('fix')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fix")
    return WorktreePath(path=repo, branch="fix/1", placeholder_sha="placeholder")


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    return tmp_path / "demo"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _scripted(*verdicts: str, setup: Path | None = None) -> tuple[Any, list[bool]]:
    """A run_demo that answers ``verdicts`` in turn, noting whether its setup came back."""
    seen_setup: list[bool] = []
    answers = iter(verdicts)

    def _set_up() -> None:
        if setup is not None:
            seen_setup.append(setup.exists())
            setup.write_text("VITE_API=http://localhost:4010\n")

    async def _run(_round: int) -> DemoRound:
        _set_up()
        return DemoRound(
            verdict=next(answers),  # type: ignore[arg-type]
            evidence="clicked Save, nothing happened",
            video_path="/tmp/demo/demo.webm",
            screenshots=["/tmp/demo/after.png"],
        )

    return _run, seen_setup


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ok_passes_and_records_the_round(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    state = DemoGateState()
    run, _ = _scripted("ok")
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    assert await hook.hooks[0](_so_input(), None, {"signal": None}) == {}
    assert state.ok is not None and state.ok.verdict == "ok"
    assert state.ok.heads == {str(worktree.path): _git(worktree.path, "rev-parse", "HEAD").strip()}


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_other_verdicts_go_back_to_the_fixer(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    state = DemoGateState()
    run, _ = _scripted("broken")
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    result = await hook.hooks[0](_so_input(), None, {"signal": None})

    assert _decision(result) == "deny"
    assert "`broken`" in _reason(result)
    assert "clicked Save, nothing happened" in _reason(result)
    assert "/tmp/demo/after.png" in _reason(result)
    assert state.ok is None and state.last is not None


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_setup_is_stashed_between_rounds_and_restored(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    env_file = worktree.path / ".env.demo"
    state = DemoGateState()
    run, seen_setup = _scripted("unavailable", "ok", setup=env_file)
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    await hook.hooks[0](_so_input("tu_1"), None, {"signal": None})
    assert not env_file.exists()
    assert _git(worktree.path, "status", "--porcelain") == ""

    # The fixer answers with a new commit; the next round gets its setup back.
    _write(worktree.path / "app.js", "console.log('fix 2')\n")
    _git(worktree.path, "commit", "-qam", "fix 2")
    await hook.hooks[0](_so_input("tu_2"), None, {"signal": None})

    assert seen_setup == [False, True]
    assert state.ok is not None
    assert "VITE_API" in state.ok.setup_diffs[str(worktree.path)]
    assert _git(worktree.path, "status", "--porcelain") == ""


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_ok_is_not_rerun_until_the_fix_moves(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    state = DemoGateState()
    run, _ = _scripted("ok")
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    await hook.hooks[0](_so_input("tu_1"), None, {"signal": None})
    assert await hook.hooks[0](_so_input("tu_2"), None, {"signal": None}) == {}
    assert state.rounds == 1


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_stops_after_max_rounds(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    state = DemoGateState()
    run, _ = _scripted(*["broken"] * (MAX_DEMO_ROUNDS + 1))
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    for i in range(MAX_DEMO_ROUNDS):
        result = await hook.hooks[0](_so_input(f"tu_{i}"), None, {"signal": None})
        assert _decision(result) == "deny"
    assert await hook.hooks[0](_so_input("tu_last"), None, {"signal": None}) == {}
    assert state.rounds == MAX_DEMO_ROUNDS


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_bypass_file_stands_the_gate_down(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    _write(demo_dir / BYPASS_FILE, "needs a Kafka cluster to render anything\n")
    state = DemoGateState()
    run, _ = _scripted("broken")
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    assert await hook.hooks[0](_so_input(), None, {"signal": None}) == {}
    assert state.bypass_reason == "needs a Kafka cluster to render anything"
    assert state.rounds == 0


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_dirty_checkout_goes_back_to_the_fixer(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    _write(worktree.path / "stray.txt", "uncommitted\n")
    state = DemoGateState()
    ran: list[int] = []

    async def _run(round_no: int) -> DemoRound:
        ran.append(round_no)
        return DemoRound(verdict="ok")

    hook = require_demo_hook([worktree], _run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)
    result = await hook.hooks[0](_so_input(), None, {"signal": None})

    assert _decision(result) == "deny"
    assert "stray.txt" in _reason(result)
    assert ran == [] and state.rounds == 1
    # Left in place: the fixer decides whether it belongs in the fix.
    assert (worktree.path / "stray.txt").exists()


@patch("src.agents.hooks.fix_missing_reason", return_value="not pushed")
async def test_waits_for_a_pushed_fix(
    _missing: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    state = DemoGateState()
    run, _ = _scripted("broken")
    hook = require_demo_hook([worktree], run, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)

    assert await hook.hooks[0](_so_input(), None, {"signal": None}) == {}
    assert state.rounds == 0


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_demo_agent_failure_goes_back_to_the_fixer(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    async def _crash(_round: int) -> DemoRound:
        _write(worktree.path / "mock.json", "{}")
        raise RuntimeError("rate limited")

    state = DemoGateState()
    hook = require_demo_hook([worktree], _crash, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)
    result = await hook.hooks[0](_so_input(), None, {"signal": None})

    assert _decision(result) == "deny"
    reason = _reason(result)
    assert "RuntimeError: rate limited" in reason
    assert f"{DEMO_AGENT_TIMEOUT_S // 60} minutes" in reason
    assert f"{MAX_DEMO_ROUNDS - 1} round(s) left" in reason
    assert state.error == "RuntimeError: rate limited"
    assert _git(worktree.path, "status", "--porcelain") == ""


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_demo_agent_past_its_budget_goes_back_to_the_fixer(
    _pushed: Any,
    worktree: WorktreePath,
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.agents.hooks.DEMO_AGENT_TIMEOUT_S", 0.05)

    async def _slow(_round: int) -> DemoRound:
        await asyncio.sleep(5)
        return DemoRound(verdict="ok")

    state = DemoGateState()
    hook = require_demo_hook([worktree], _slow, state, ctx=_ctx(tmp_path), demo_dir=demo_dir)
    result = await hook.hooks[0](_so_input(), None, {"signal": None})

    assert _decision(result) == "deny"
    assert "budget" in state.error


# ---------------------------------------------------------------------------
# GateResults — the CLI runs matching hooks in parallel
# ---------------------------------------------------------------------------


def _gate(answer: dict[str, Any], delay: float = 0.0) -> HookMatcher:
    async def _hook(_input: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        await asyncio.sleep(delay)
        return answer

    return HookMatcher(matcher="StructuredOutput", hooks=[_hook], timeout=10)


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_demo_stands_down_when_ci_denies_the_same_call(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    gates = GateResults()
    deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
    ci_red = gates.track(_gate(deny, delay=0.05))
    ci_green = gates.track(_gate({}))
    state = DemoGateState()
    run, _ = _scripted("broken")
    demo = require_demo_hook(
        [worktree], run, state, ctx=_ctx(tmp_path), ci_gates=gates, demo_dir=demo_dir
    )

    results = await asyncio.gather(
        demo.hooks[0](_so_input(), None, {"signal": None}),
        ci_red.hooks[0](_so_input(), None, {"signal": None}),
        ci_green.hooks[0](_so_input(), None, {"signal": None}),
    )

    assert results[0] == {}
    assert state.rounds == 0


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_demo_runs_once_every_ci_gate_passed(
    _pushed: Any, worktree: WorktreePath, demo_dir: Path, tmp_path: Path
) -> None:
    gates = GateResults()
    ci = gates.track(_gate({}, delay=0.05))
    state = DemoGateState()
    run, _ = _scripted("broken")
    demo = require_demo_hook(
        [worktree], run, state, ctx=_ctx(tmp_path), ci_gates=gates, demo_dir=demo_dir
    )

    demo_result, _ = await asyncio.gather(
        demo.hooks[0](_so_input(), None, {"signal": None}),
        ci.hooks[0](_so_input(), None, {"signal": None}),
    )

    assert _decision(demo_result) == "deny"
    assert state.rounds == 1


# ---------------------------------------------------------------------------
# forbid_publishing_hook
# ---------------------------------------------------------------------------


def _bash(command: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize(
    "command",
    [
        "git push origin fix/1",
        "git -C /work/app commit -am wip",
        "cd app && git stash",
        "gh pr edit 9 --body x",
        "glab mr note 3 -m hi",
    ],
)
async def test_publishing_commands_are_denied(command: str) -> None:
    result = await forbid_publishing_hook().hooks[0](_bash(command), None, {"signal": None})
    assert _decision(result) == "deny"


@pytest.mark.parametrize(
    "command",
    ["git status", "git diff HEAD~1", "pnpm install --frozen-lockfile", "echo 'git push'"],
)
async def test_other_commands_pass(command: str) -> None:
    assert await forbid_publishing_hook().hooks[0](_bash(command), None, {"signal": None}) == {}
