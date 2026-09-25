"""Output-schema validation and agent structure tests for the issue triage agent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage
from pydantic import ValidationError

from src.agents.issue import (
    IssueFixerAgent,
    IssueFixerInput,
    TriageAgent,
    TriageInput,
    TriageOutput,
)
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


# ── TriageOutput schema tests ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind",
    ["proceed", "needs_info", "push_back", "duplicate", "already_fixed", "refuse", "split"],
)
def test_triage_output_accepts_each_kind(kind: str) -> None:
    output = TriageOutput.model_validate({"kind": kind, "reasoning": "ok", "comment_body": ""})
    assert output.kind == kind


def test_triage_output_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TriageOutput.model_validate({"kind": "delete_repo"})


def test_triage_output_defaults() -> None:
    output = TriageOutput.model_validate({"kind": "proceed"})
    assert output.reasoning == ""
    assert output.comment_body == ""
    assert output.target_repos == []
    assert output.findings == ""


def test_triage_output_accepts_target_repos_and_findings() -> None:
    output = TriageOutput.model_validate(
        {
            "kind": "proceed",
            "target_repos": ["jdoe_ingester", "jdoe_frontend"],
            "findings": "root cause is in legacy.rs",
        }
    )
    assert output.target_repos == ["jdoe_ingester", "jdoe_frontend"]
    assert output.findings == "root cause is in legacy.rs"


# ── Agent structure tests ──────────────────────────────────────────────────


def test_triage_agent_resolves_prompt_path() -> None:
    expected = Path(__file__).resolve().parents[2] / "src" / "prompts" / "issue"
    agent = TriageAgent()
    assert agent._prompts_dir() == expected
    assert (expected / "triage.md").is_file()


def test_fixer_agent_resolves_prompt_path() -> None:
    expected = Path(__file__).resolve().parents[2] / "src" / "prompts" / "issue"
    agent = IssueFixerAgent()
    assert agent._prompts_dir() == expected
    assert (expected / "fixer.md").is_file()


def test_fixer_renders_the_answer_prompt_without_a_code_change() -> None:
    agent = IssueFixerAgent()
    answer = agent._render(
        IssueFixerInput(code_change=False, issue_body="Top 30 IPs by registrations")
    )
    fix = agent._render(IssueFixerInput(branch="jeanclode/issue-1"))
    assert "Top 30 IPs by registrations" in answer
    assert "no pull/merge request" in answer
    assert "git push -f origin jeanclode/issue-1" in fix


def test_triage_output_defaults_to_a_code_change() -> None:
    assert TriageOutput(kind="proceed").code_change is True


# ── TriageAgent invoke tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_agent_returns_structured(tmp_path: Path) -> None:
    structured = {"kind": "proceed", "reasoning": "clear issue", "comment_body": ""}
    with patch("src.agents.base.query", side_effect=lambda **_kw: _stream(structured)()):
        result = await TriageAgent().invoke(TriageInput(), _ctx(tmp_path))
    assert result.structured == structured


@pytest.mark.asyncio
async def test_triage_agent_renders_inputs_via_jinja(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        return _stream(
            {"kind": "needs_info", "reasoning": "missing context", "comment_body": "What version?"}
        )()

    inputs = TriageInput(
        issue_url="https://github.com/org/repo/issues/42",
        provider="github",
        repo="org/repo",
        issue_number="42",
        issue_title="Widget crashes on startup",
        issue_body="The widget crashes with a NullPointerException.",
        comments="**alice:** Can you add a stack trace?",
    )
    with patch("src.agents.base.query", side_effect=fake_query):
        await TriageAgent().invoke(inputs, _ctx(tmp_path))

    prompt = captured["prompt"]
    assert "https://github.com/org/repo/issues/42" in prompt
    assert "Widget crashes on startup" in prompt
    assert "NullPointerException" in prompt
    assert "alice" in prompt
    assert "{{" not in prompt


@pytest.mark.parametrize(
    "kind",
    ["proceed", "needs_info", "push_back", "duplicate", "already_fixed", "refuse", "split"],
)
@pytest.mark.asyncio
async def test_triage_agent_all_7_outcomes_reachable(tmp_path: Path, kind: str) -> None:
    structured = {
        "kind": kind,
        "reasoning": f"reason for {kind}",
        "comment_body": "" if kind == "proceed" else f"comment for {kind}",
    }
    with patch("src.agents.base.query", side_effect=lambda **_kw: _stream(structured)()):
        result = await TriageAgent().invoke(
            TriageInput(issue_url="https://github.com/o/r/issues/1"), _ctx(tmp_path)
        )
    assert result.structured is not None
    output = TriageOutput.model_validate(result.structured)
    assert output.kind == kind
