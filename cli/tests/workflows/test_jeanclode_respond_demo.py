"""The demo gate on a respond turn: armed by the planner's ``demo_plan``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.activities.demo import DEMO_END, DEMO_START
from src.activities.git.schemas import WorktreePath
from src.activities.respond.schemas import MentionContext
from src.agents.respond import PlannerAgent, PlannerInput
from src.agents.schemas import AgentResult
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.workflows.jeanclode_respond.demo import RespondDemo
from src.workflows.jeanclode_respond.utils import exclude_context_dir


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feat/x")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.js").write_text("console.log('ui')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "ui")
    return repo


def _so_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "StructuredOutput",
        "tool_input": tool_input,
        "tool_use_id": "tu_1",
    }


def _demo(checkout: Path, tmp_path: Path, *, description: str = "") -> RespondDemo:
    mention = MentionContext(
        platform="github",
        repo="acme/app",
        pr="7",
        mention_body="add a demo of this",
        mention_author="alice",
    )
    return RespondDemo(
        WorktreePath(path=checkout, branch="feat/x"),
        _git(checkout, "rev-parse", "HEAD").strip(),
        mention,
        "https://github.com/acme/app/pull/7",
        description,
        "diff --git a/app.js b/app.js",
        "",
        ctx=RunContext(cwd=checkout, workspace=tmp_path, events=EventBus()),
        demo_dir=tmp_path / "demo",
    )


def _verdict(verdict: str) -> AgentResult:
    return AgentResult(
        structured={"verdict": verdict, "evidence": "saw it", "app_repo": "acme/app"}
    )


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_no_demo_plan_finishes_without_a_demo(
    _pushed: Any, checkout: Path, tmp_path: Path
) -> None:
    demo = _demo(checkout, tmp_path)
    with patch("src.workflows.jeanclode_respond.demo.DemoAgent.invoke") as invoke:
        result = await demo.hook().hooks[0](
            _so_input({"actions_taken": ["handle"]}), "tu_1", {"signal": None}
        )

    assert result == {}
    invoke.assert_not_called()
    assert demo.plan == ""
    assert demo.state.rounds == 0


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_demo_plan_runs_the_demo_agent_on_the_pr(
    _pushed: Any, checkout: Path, tmp_path: Path
) -> None:
    old_block = f"{DEMO_START}\nold video\n{DEMO_END}"
    demo = _demo(checkout, tmp_path, description=f"{old_block}\n\nAdds a badge.")
    invoke = AsyncMock(return_value=_verdict("ok"))
    with patch("src.workflows.jeanclode_respond.demo.DemoAgent.invoke", invoke):
        result = await demo.hook().hooks[0](
            _so_input({"actions_taken": ["demo"], "demo_plan": "open /orders, see the badge"}),
            "tu_1",
            {"signal": None},
        )

    assert result == {}
    assert demo.plan == "open /orders, see the badge"
    assert demo.state.ok is not None
    demo_input = invoke.call_args.args[0]
    assert demo_input.source == "pr"
    assert demo_input.demo_plan == "open /orders, see the badge"
    assert demo_input.issue_url == "https://github.com/acme/app/pull/7"
    # The old recording is not something the demo agent should read as the PR's intent.
    assert "old video" not in demo_input.issue_body
    assert "add a demo of this" in demo_input.comments


@patch("src.agents.hooks.fix_missing_reason", return_value=None)
async def test_a_failed_demo_goes_back_to_the_planner(
    _pushed: Any, checkout: Path, tmp_path: Path
) -> None:
    demo = _demo(checkout, tmp_path)
    with patch(
        "src.workflows.jeanclode_respond.demo.DemoAgent.invoke",
        AsyncMock(return_value=_verdict("broken")),
    ):
        result = await demo.hook().hooks[0](
            _so_input({"actions_taken": ["demo"], "demo_plan": "show it"}),
            "tu_1",
            {"signal": None},
        )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert demo.state.last is not None and demo.state.last.verdict == "broken"


def test_exclude_context_dir_keeps_the_cache_out_of_git_status(checkout: Path) -> None:
    (checkout / ".context").mkdir()
    (checkout / ".context" / "diff").write_text("x")

    exclude_context_dir(checkout)
    exclude_context_dir(checkout)

    assert _git(checkout, "status", "--porcelain") == ""
    exclude = (checkout / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/.context/") == 1


def test_planner_prompt_drops_the_demo_action_when_the_org_turned_it_off() -> None:
    on = PlannerAgent()._render(PlannerInput(pr="7", demo_enabled=True))
    off = PlannerAgent()._render(PlannerInput(pr="7", demo_enabled=False))

    assert "demo_plan" in on
    assert "add a demo of this" in on
    assert "demo_plan" not in off
    assert "add a demo of this" not in off


def test_planner_prompt_asks_to_re_record_only_an_existing_demo() -> None:
    with_demo = PlannerAgent()._render(PlannerInput(pr="7", has_demo=True))
    without = PlannerAgent()._render(PlannerInput(pr="7", has_demo=False))

    assert "re-record it even though nobody asked" in with_demo
    assert "re-record it even though nobody asked" not in without
