from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from src.agents.review import (
    AnalyzerAgent,
    AnalyzerInput,
    DeduplicatorAgent,
    DeduplicatorInput,
    FactCheckerAgent,
    FactCheckerInput,
    IssueExplorerAgent,
    IssueExplorerInput,
    StylerAgent,
    StylerInput,
    SynthesizerAgent,
    SynthesizerInput,
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


def _fake_query(text: str, structured: Any = None):
    def factory(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield _result_message(text, structured)

        return gen()

    return factory


@pytest.mark.asyncio
async def test_issue_explorer_parses_structured_output(ctx: RunContext) -> None:
    structured = {"context": "Linked Issue #1", "issue_refs": ["http://x/1"]}
    with patch("src.agents.base.query", side_effect=_fake_query("ok", structured)):
        result = await IssueExplorerAgent().invoke(
            IssueExplorerInput(platform="github", repo="o/r", pr_description="d"),
            ctx,
        )
    assert result.structured == structured


@pytest.mark.asyncio
async def test_analyzer_returns_text_findings(ctx: RunContext) -> None:
    payload = '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}'
    with patch("src.agents.base.query", side_effect=_fake_query(payload)):
        result = await AnalyzerAgent().invoke(
            AnalyzerInput(platform="github", pr_description="d", diff="diff"),
            ctx,
        )
    assert "comments" in result.text


@pytest.mark.asyncio
async def test_synthesizer_round_trips(ctx: RunContext) -> None:
    payload = '{"comments":[]}'
    with patch("src.agents.base.query", side_effect=_fake_query(payload)):
        result = await SynthesizerAgent().invoke(
            SynthesizerInput(
                platform="github",
                pr_description="d",
                diff="diff",
                findings_json='{"comments":[]}',
            ),
            ctx,
        )
    assert result.text == payload


@pytest.mark.asyncio
async def test_fact_checker_parses_keep_indices(ctx: RunContext) -> None:
    structured = {"keep_indices": [0, 2]}
    with patch("src.agents.base.query", side_effect=_fake_query("ok", structured)):
        result = await FactCheckerAgent().invoke(
            FactCheckerInput(pr_description="d", diff="diff", comments_json="[]"),
            ctx,
        )
    assert result.structured == structured


@pytest.mark.asyncio
async def test_deduplicator_parses_keep_indices(ctx: RunContext) -> None:
    structured = {"keep_indices": [1]}
    with patch("src.agents.base.query", side_effect=_fake_query("ok", structured)):
        result = await DeduplicatorAgent().invoke(
            DeduplicatorInput(discussions="", comments_json="[]"), ctx
        )
    assert result.structured == structured


@pytest.mark.asyncio
async def test_styler_parses_bodies(ctx: RunContext) -> None:
    structured = {"bodies": ["a", "b"]}
    with patch("src.agents.base.query", side_effect=_fake_query("ok", structured)):
        result = await StylerAgent().invoke(StylerInput(comments_json="[]"), ctx)
    assert result.structured == structured


@pytest.mark.asyncio
async def test_prompt_inlines_diff_via_template(ctx: RunContext) -> None:
    captured: dict[str, str] = {}

    def factory(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        captured["prompt"] = prompt

        async def gen() -> AsyncIterator[Any]:
            yield _result_message("ok", {"comments": []})

        return gen()

    with patch("src.agents.base.query", side_effect=factory):
        await AnalyzerAgent().invoke(
            AnalyzerInput(
                platform="github",
                pr_description="MARKER_DESC",
                diff="MARKER_DIFF",
                issue_context="MARKER_ISSUE",
            ),
            ctx,
        )

    assert "MARKER_DESC" in captured["prompt"]
    assert "MARKER_DIFF" in captured["prompt"]
    assert "MARKER_ISSUE" in captured["prompt"]
    assert "cat .context" not in captured["prompt"]
