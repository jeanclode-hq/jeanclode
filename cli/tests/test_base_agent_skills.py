"""BaseAgent — opt-in third-party skill wiring.

When ``use_third_party_skills`` is True and the run loaded skills:
  - the Skill tool ends up in allowed_tools
  - the user's plugin folders are passed via ClaudeAgentOptions.plugins
  - a discovery block is appended to the prompt so the model sees what's
    available to invoke
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from src.agents.base import BaseAgent
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.skills.schemas import Skill


class _Input(BaseModel):
    message: str


def _make_agent(tmp_path: Path, *, use_third_party_skills: bool = False) -> type[BaseAgent]:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "p.md").write_text("Task: {{ message }}")

    class _A(BaseAgent):
        name: ClassVar[str] = "a"
        prompt_file: ClassVar[str] = str(prompts / "p.md")
        allowed_tools: ClassVar[list[str]] = ["Read"]
        max_turns: ClassVar[int] = 1

    _A.use_third_party_skills = use_third_party_skills
    return _A


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result="ok",
    )


def _stream() -> Any:
    async def gen() -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield _result_message()

    return gen()


def _make_skill(tmp_path: Path, plugin_name: str, skill_name: str, description: str) -> Skill:
    plugin = tmp_path / plugin_name
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(f'{{"name": "{plugin_name}"}}')
    sd = plugin / "skills" / skill_name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: {description}\n---\nbody")
    return Skill(name=skill_name, description=description, skill_dir=sd)


async def _capture(agent_cls: type[BaseAgent], ctx: RunContext) -> tuple[str, Any]:
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt
        captured["options"] = options
        return _stream()

    with patch("src.agents.base.query", side_effect=fake_query):
        await agent_cls().invoke(_Input(message="hi"), ctx)

    return captured["prompt"], captured["options"]


async def test_opt_out_means_no_skill_wiring_anywhere(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path)
    skill = _make_skill(tmp_path, "p", "ruff", "ruff style")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), skills=[skill])

    prompt, options = await _capture(Agent, ctx)

    assert "User-loaded skills" not in prompt
    assert "Skill" not in (options.allowed_tools or [])
    assert not getattr(options, "plugins", None)


async def test_opt_in_with_no_skills_loaded_is_no_op(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_third_party_skills=True)
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())

    prompt, options = await _capture(Agent, ctx)

    assert "User-loaded skills" not in prompt
    assert "Skill" not in (options.allowed_tools or [])


async def test_opt_in_with_skills_wires_tool_plugins_and_prompt(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_third_party_skills=True)
    skill = _make_skill(tmp_path, "p", "ruff", "ruff style")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), skills=[skill])

    prompt, options = await _capture(Agent, ctx)

    # Prompt: agent's own task + the discovery block
    assert "Task: hi" in prompt
    assert "User-loaded skills" in prompt
    assert "**ruff** — ruff style" in prompt
    assert 'Skill(name="<name>")' in prompt
    assert "Per <skill-name> skill:" in prompt

    # Options: Skill tool authorized + plugin folder passed
    assert "Skill" in (options.allowed_tools or [])
    plugins = options.plugins or []
    assert any(entry["path"] == str(skill.plugin_path) for entry in plugins)


async def test_two_skills_in_different_plugins_register_both_plugins(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_third_party_skills=True)
    s1 = _make_skill(tmp_path, "p1", "ruff", "ruff style")
    s2 = _make_skill(tmp_path, "p2", "sec", "security")
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), skills=[s1, s2])

    _, options = await _capture(Agent, ctx)

    plugin_paths = {entry["path"] for entry in (options.plugins or [])}
    assert plugin_paths == {str(s1.plugin_path), str(s2.plugin_path)}


async def test_two_skills_in_same_plugin_register_plugin_once(tmp_path: Path) -> None:
    Agent = _make_agent(tmp_path, use_third_party_skills=True)
    s1 = _make_skill(tmp_path, "p", "a", "A")
    s2 = _make_skill(tmp_path, "p", "b", "B")  # same plugin folder
    ctx = RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus(), skills=[s1, s2])

    _, options = await _capture(Agent, ctx)

    plugin_paths = [entry["path"] for entry in (options.plugins or [])]
    assert plugin_paths == [str(s1.plugin_path)]
