"""Fixer LLM choice: parsing the backend's options, resolving triage's answer,
retargeting the fixer session, and the triage prompt wiring (#43)."""

import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultError

from src.agents.base import BaseAgent
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.llm_options import (
    LLMOption,
    apply_fixer_llm,
    load_llm_options_from_env,
    resolve_fixer_llm,
)
from tests.test_base_agent_skills import _capture, _Input

DEFAULT = LLMOption(
    id="c1", name="claude", provider="claude_code", model_high="sonnet", model_heavy="opus"
)
SELF_HOSTED = LLMOption(
    id="c2",
    name="self-hosted",
    provider="openai_compatible",
    model_high="qwen-coder",
    model_low="qwen-small",
    env={"ANTHROPIC_BASE_URL": "https://llm.internal/v1", "CLAUDE_CODE_OAUTH_TOKEN": ""},
    secret_env={"ANTHROPIC_AUTH_TOKEN": "JEANCLODE_LLM_OPTION_1_SECRET"},
)
OPTIONS = [DEFAULT, SELF_HOSTED]


def _ctx(tmp_path: Path, **kw: Any) -> RunContext:
    return RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        model="sonnet",
        small_model="haiku",
        **kw,
    )


def test_load_options_from_env() -> None:
    raw = json.dumps([o.model_dump() for o in OPTIONS])
    assert load_llm_options_from_env({"JEANCLODE_LLM_OPTIONS": raw}) == OPTIONS


@pytest.mark.parametrize("raw", ["", "not json", '[{"id": 1}]', '{"id": "x"}'])
def test_missing_or_malformed_options_mean_no_choice(raw: str) -> None:
    assert load_llm_options_from_env({"JEANCLODE_LLM_OPTIONS": raw}) == []


def test_no_options_resolves_to_untouched_default(tmp_path: Path) -> None:
    choice = resolve_fixer_llm([], "self-hosted", "heavy")
    ctx = _ctx(tmp_path)
    assert apply_fixer_llm(ctx, choice) is ctx
    assert choice.summary == ""


def test_default_high_leaves_the_session_untouched(tmp_path: Path) -> None:
    choice = resolve_fixer_llm(OPTIONS, "", "high")
    ctx = _ctx(tmp_path)
    assert apply_fixer_llm(ctx, choice) is ctx
    assert choice.summary == ""


def test_default_heavy_only_swaps_the_model(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, env={"FOO": "1"})
    fixer = apply_fixer_llm(ctx, resolve_fixer_llm(OPTIONS, "claude", "heavy"))
    assert fixer.model == "opus"
    assert fixer.env == {"FOO": "1"}
    assert fixer.small_model == "haiku"


def test_other_credential_retargets_env_model_and_credential_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEANCLODE_LLM_OPTION_1_SECRET", "sandbox-proxy-injected")
    choice = resolve_fixer_llm(OPTIONS, "self-hosted", "high", "the issue asks for it")
    fixer = apply_fixer_llm(_ctx(tmp_path, env={"FOO": "1"}), choice)

    assert fixer.model == "qwen-coder"
    assert fixer.small_model == "qwen-small"
    assert fixer.env == {
        "FOO": "1",
        "ANTHROPIC_BASE_URL": "https://llm.internal/v1",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_AUTH_TOKEN": "sandbox-proxy-injected",
        "JEANCLODE_LLM_CREDENTIAL_ID": "c2",
    }
    assert choice.summary == "self-hosted (qwen-coder) — the issue asks for it"


def test_unknown_credential_keeps_default_and_says_so(tmp_path: Path) -> None:
    choice = resolve_fixer_llm(OPTIONS, "private-gpu", "high")
    ctx = _ctx(tmp_path)
    assert apply_fixer_llm(ctx, choice) is ctx
    assert "`private-gpu` is not configured" in choice.summary


def test_heavy_without_heavy_model_falls_back_to_high() -> None:
    choice = resolve_fixer_llm(OPTIONS, "self-hosted", "heavy")
    assert choice.tier == "high"
    assert "no heavy model" in choice.note


def _chooser(tmp_path: Path) -> type[BaseAgent]:
    prompt = tmp_path / "p.md"
    prompt.write_text("Task: {{ message }}")

    class _Triage(BaseAgent):
        name: ClassVar[str] = "t"
        prompt_file: ClassVar[str] = str(prompt)
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1
        choose_fixer_llm: ClassVar[bool] = True

    return _Triage


async def test_triage_without_options_is_unchanged(tmp_path: Path) -> None:
    prompt, _ = await _capture(_chooser(tmp_path), _ctx(tmp_path))
    assert "Fixer model choice" not in prompt


async def test_triage_with_options_gets_the_choice(tmp_path: Path) -> None:
    prompt, _ = await _capture(_chooser(tmp_path), _ctx(tmp_path, llm_options=OPTIONS))
    assert "Fixer model choice" in prompt
    assert "**self-hosted** (openai_compatible) — model: qwen-coder" in prompt
    assert "heavy model: opus" in prompt


async def test_failed_session_carries_its_credential(tmp_path: Path) -> None:
    ctx = apply_fixer_llm(_ctx(tmp_path), resolve_fixer_llm(OPTIONS, "self-hosted", "high"))
    err = ResultError("rate limited", {"subtype": "success", "api_error_status": 429})

    def boom(**_: Any) -> Any:
        raise err

    with patch("src.agents.base.query", side_effect=boom), pytest.raises(ResultError) as info:
        await _chooser(tmp_path)().invoke(_Input(message="hi"), ctx)
    assert info.value.llm_credential_id == "c2"  # type: ignore[attr-defined]
