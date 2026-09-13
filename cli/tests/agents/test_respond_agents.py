"""Output-schema validation for PlannerAgent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage
from pydantic import ValidationError

from src.agents.respond import PlannerAgent, PlannerInput, PlannerOutput
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _result_message(structured: dict) -> ResultMessage:
    msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result="",
    )
    object.__setattr__(msg, "structured_output", structured)
    return msg


def _stream(structured: dict) -> Any:
    async def gen() -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield _result_message(structured)

    return gen


@pytest.mark.parametrize("action_taken", ["route", "handle"])
def test_planner_output_accepts_each_action(action_taken: str) -> None:
    output = PlannerOutput.model_validate({"actions_taken": [action_taken], "summary": "ok"})
    assert output.actions_taken == [action_taken]


def test_planner_output_accepts_multiple_compatible_actions() -> None:
    output = PlannerOutput.model_validate({"actions_taken": ["handle", "route"], "summary": "ok"})
    assert output.actions_taken == ["handle", "route"]


def test_planner_output_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate({"actions_taken": ["delete_repo"]})


def test_planner_output_rejects_empty_actions() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate({"actions_taken": []})


def test_planner_agent_resolves_prompt_path() -> None:
    expected = Path(__file__).resolve().parents[2] / "src" / "prompts" / "respond"
    agent = PlannerAgent()
    assert agent._prompts_dir() == expected
    assert (expected / "planner.md").is_file()


@pytest.mark.asyncio
async def test_planner_agent_returns_structured(tmp_path: Path) -> None:
    structured = {"actions_taken": ["handle"], "summary": "replied to thread"}
    with patch("src.agents.base.query", side_effect=lambda **_kw: _stream(structured)()):
        result = await PlannerAgent().invoke(PlannerInput(), _ctx(tmp_path))
    assert result.structured == structured


@pytest.mark.asyncio
async def test_planner_agent_renders_inputs_via_jinja(tmp_path: Path) -> None:
    """Mention/PR fields are inlined as Jinja vars — the agent never has
    to ``cat`` files from disk at run time."""
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        return _stream({"actions_taken": ["handle"], "summary": "ok"})()

    inputs = PlannerInput(
        platform="github",
        repo="o/r",
        pr="42",
        surface="pr_inline_thread",
        target_url="https://github.com/o/r/pull/42#discussion_r42",
        mention_body="please rename Foo to Bar",
        mention_author="alice",
        thread_id="PRRT_abc",
        comment_id="42",
        pr_author="bob",
        pr_description="# Title\n\nbody",
        diff="@@ -1 +1 @@\n-old\n+new",
        discussions="some thread",
    )
    with patch("src.agents.base.query", side_effect=fake_query):
        await PlannerAgent().invoke(inputs, _ctx(tmp_path))

    prompt = captured["prompt"]
    assert "pr_inline_thread" in prompt
    assert "please rename Foo to Bar" in prompt
    assert "PRRT_abc" in prompt
    assert "alice" in prompt
    assert "@@ -1 +1 @@" in prompt
    assert "{{" not in prompt


@pytest.mark.asyncio
async def test_planner_receives_resolve_guidance_with_issue_mention(tmp_path: Path) -> None:
    """A resolution-intent mention on an issue renders into a prompt that
    also carries the jeanclode:resolve routing instructions."""
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        return _stream({"actions_taken": ["route"], "summary": "labelled jeanclode:resolve"})()

    inputs = PlannerInput(
        platform="github",
        repo="o/r",
        issue="12",
        surface="issue",
        mention_body="could you take care of this one?",
        mention_author="alice",
    )
    with patch("src.agents.base.query", side_effect=fake_query):
        result = await PlannerAgent().invoke(inputs, _ctx(tmp_path))

    prompt = captured["prompt"]
    assert "could you take care of this one?" in prompt
    assert "jeanclode:resolve" in prompt
    assert result.structured == {
        "actions_taken": ["route"],
        "summary": "labelled jeanclode:resolve",
    }
