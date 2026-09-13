"""Sentry agents — prompt rendering + structured output parsing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from src.activities.sentry import TriagedIssue, TriageOutput
from src.agents.sentry import (
    FixerAgent,
    FixerInput,
    SynthesisAgent,
    SynthesisInput,
    TriageAgent,
    TriageInput,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _result_message(text: str = "ok", structured: Any = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result=text,
        structured_output=structured,
    )


def _stream(messages: list[Any]) -> Any:
    async def gen() -> AsyncIterator[Any]:
        for m in messages:
            await asyncio.sleep(0)
            yield m

    return gen()


async def _capture_prompt(agent: Any, agent_input: Any, ctx: RunContext) -> str:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        return _stream([_result_message()])

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent.invoke(agent_input, ctx)
    return captured["prompt"]


async def test_triage_renders_formatted_block(ctx: RunContext) -> None:
    prompt = await _capture_prompt(
        TriageAgent(),
        TriageInput(
            issue_id="9",
            sentry_url="https://sentry.io/issues/9",
            formatted="## Pre-fetched Sentry Data\nERROR DETAIL",
        ),
        ctx,
    )
    assert "ERROR DETAIL" in prompt
    assert "## Pre-fetched Sentry Data" in prompt


async def test_triage_parses_structured_output(ctx: RunContext) -> None:
    structured = TriageOutput(
        kind="proceed",
        confidence=0.9,
        root_cause_hypothesis="missing null",
        category="null_reference",
        findings="edit foo.py",
        target_repos=["repo"],
    ).model_dump()

    def fake_query(*, prompt: str, options: Any) -> Any:
        return _stream([_result_message(structured=structured)])

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await TriageAgent().invoke(
            TriageInput(issue_id="1", sentry_url="u", formatted="data"), ctx
        )
    parsed = TriageOutput.model_validate(result.structured)
    assert parsed.kind == "proceed"
    assert parsed.actionable is True  # computed, for the backend's two-state enum
    assert parsed.target_repos == ["repo"]
    assert parsed.category == "null_reference"


def test_structured_output_tool_summary_records_the_handover() -> None:
    """When a triage's verdict is lost between the tool call and the parse,
    the logged summary is the only evidence of what it actually handed over."""
    from claude_agent_sdk import ToolUseBlock

    from src.agents.utils import tool_summary

    block = ToolUseBlock(
        id="t1",
        name="StructuredOutput",
        input={"kind": "proceed", "findings": "x" * 5000, "target_repos": ["repo"]},
    )
    summary = tool_summary(block)
    assert "kind=proceed" in summary
    assert "target_repos" in summary
    assert "x" * 100 not in summary  # keys only, never the payload


async def test_synthesis_lists_issues_in_prompt(ctx: RunContext) -> None:
    actionable = [
        TriagedIssue(
            issue_id="11",
            sentry_url="https://sentry.io/issues/11",
            triage=TriageOutput(
                kind="proceed",
                confidence=0.9,
                root_cause_hypothesis="null check",
                affected_files=["a.py"],
                category="null_reference",
                findings="guard the None case in a.py",
            ),
            target_repos=["repo"],
        ),
        TriagedIssue(
            issue_id="22",
            sentry_url="https://sentry.io/issues/22",
            triage=TriageOutput(
                kind="proceed",
                confidence=0.8,
                root_cause_hypothesis="off by one",
                affected_files=["b.py"],
                category="logic_error",
                findings="fix the loop bound in b.py",
            ),
            target_repos=["repo"],
        ),
    ]
    prompt = await _capture_prompt(SynthesisAgent(), SynthesisInput(actionable=actionable), ctx)
    assert "Issue 11" in prompt
    assert "Issue 22" in prompt
    assert "null check" in prompt
    assert "off by one" in prompt
    # synthesis groups on the planned fix, so triage's findings must reach it
    assert "guard the None case in a.py" in prompt
    assert "fix the loop bound in b.py" in prompt


async def test_fixer_includes_pr_url_and_findings(ctx: RunContext) -> None:
    prompt = await _capture_prompt(
        FixerAgent(),
        FixerInput(
            sentry_urls=["https://sentry.io/issues/3"],
            branch="fix/x",
            findings="Step 1: open file.\nStep 2: edit.",
            repos=[
                {
                    "name": "repo",
                    "path": "/w/repo",
                    "pr_url": "https://github.com/org/repo/pull/7",
                    "ci_bypass_path": "/w/repo/.jc-bypass",
                }
            ],
        ),
        ctx,
    )
    assert "https://github.com/org/repo/pull/7" in prompt
    assert "fix/x" in prompt
    assert "Step 1: open file." in prompt


async def test_fixer_lists_every_repo_with_its_own_pr(ctx: RunContext) -> None:
    """Multi-repo groups: each worktree path, PR and bypass file must be
    addressable, or the fixer pushes into the wrong checkout."""
    prompt = await _capture_prompt(
        FixerAgent(),
        FixerInput(
            sentry_urls=["https://sentry.io/issues/3"],
            branch="fix/x",
            findings="change both sides of the contract",
            repos=[
                {
                    "name": "api",
                    "path": "/w/wt/api",
                    "pr_url": "https://gitlab.example/api/-/merge_requests/1",
                    "ci_bypass_path": "/w/wt/api/.jc-bypass",
                },
                {
                    "name": "front",
                    "path": "/w/wt/front",
                    "pr_url": "https://gitlab.example/front/-/merge_requests/2",
                    "ci_bypass_path": "/w/wt/front/.jc-bypass",
                },
            ],
        ),
        ctx,
    )
    assert "/w/wt/api" in prompt
    assert "/w/wt/front" in prompt
    assert "https://gitlab.example/api/-/merge_requests/1" in prompt
    assert "https://gitlab.example/front/-/merge_requests/2" in prompt
    assert "/w/wt/front/.jc-bypass" in prompt
