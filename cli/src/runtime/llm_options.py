"""LLM credentials triage may pick for the fixer session (#43).

The backend sends ``JEANCLODE_LLM_OPTIONS`` only when there is a real choice:
more than one distinct credential, or a ``model_heavy`` tier on one. The first
entry is the run's default credential and carries no env overrides, so a
fixer that stays on it runs exactly like every other agent.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Literal

from jinja2 import Template
from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from src.runtime.context import RunContext

logger = logging.getLogger(__name__)

Tier = Literal["high", "heavy"]


class LLMOption(BaseModel):
    id: str
    name: str
    provider: str
    model_high: str = ""
    model_heavy: str = ""
    model_low: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    # Session env var -> container env var holding its (proxy-placeholder) value.
    secret_env: dict[str, str] = Field(default_factory=dict)

    @property
    def is_default(self) -> bool:
        return not self.env and not self.secret_env


class FixerLLMChoice(BaseModel):
    """What the fixer runs on, and the line the run reports about it."""

    option: LLMOption | None = None
    tier: Tier = "high"
    note: str = ""

    @property
    def label(self) -> str:
        if self.option is None:
            return "default"
        model = self.option.model_heavy if self.tier == "heavy" else self.option.model_high
        return f"{self.option.name} ({model or 'default model'})"

    @property
    def summary(self) -> str:
        """One line for run output and the PR body; empty when nothing is worth saying."""
        if self.option is None:
            return ""
        if self.option.is_default and self.tier == "high" and not self.note:
            return ""
        return f"{self.label} — {self.note}" if self.note else self.label


def load_llm_options_from_env(env: dict[str, str] | None = None) -> list[LLMOption]:
    raw = (env if env is not None else os.environ).get("JEANCLODE_LLM_OPTIONS", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [LLMOption.model_validate(item) for item in data]
    except ValueError, TypeError, ValidationError:
        logger.warning("ignoring malformed JEANCLODE_LLM_OPTIONS", exc_info=True)
        return []


def resolve_fixer_llm(
    options: list[LLMOption], credential: str, tier: str, reason: str = ""
) -> FixerLLMChoice:
    """Map triage's answer onto a configured option, falling back to the default.

    Anything triage names that isn't configured keeps the default and says
    so, rather than failing the run over a routing preference.
    """
    if not options:
        return FixerLLMChoice()
    default = options[0]
    notes: list[str] = []
    option = default
    if credential and credential != default.name:
        match = next((o for o in options if o.name == credential), None)
        if match is None:
            notes.append(f"credential `{credential}` is not configured, kept `{default.name}`")
        else:
            option = match
    chosen_tier: Tier = "heavy" if tier == "heavy" else "high"
    if chosen_tier == "heavy" and not option.model_heavy:
        notes.append(f"`{option.name}` has no heavy model, kept its default model")
        chosen_tier = "high"
    if reason:
        notes.insert(0, reason)
    return FixerLLMChoice(option=option, tier=chosen_tier, note="; ".join(notes))


def apply_fixer_llm(ctx: RunContext, choice: FixerLLMChoice) -> RunContext:
    """Return ``ctx`` retargeted at the chosen credential and tier."""
    option = choice.option
    if option is None:
        return ctx
    if option.is_default and choice.tier == "high":
        return ctx
    model = option.model_heavy if choice.tier == "heavy" else option.model_high
    update: dict[str, object] = {"model": model or ctx.model}
    if not option.is_default:
        env = {**ctx.env, **option.env}
        for session_var, container_var in option.secret_env.items():
            env[session_var] = os.environ.get(container_var, "")
        env["JEANCLODE_LLM_CREDENTIAL_ID"] = option.id
        update["env"] = env
        update["small_model"] = option.model_low or ctx.small_model
    return ctx.model_copy(update=update)


_BLOCK = Template(
    """\
=== Fixer model choice ===
You also decide which LLM the fixer agent runs on. Fill `fixer_llm_credential`
and `fixer_llm_tier` only when `kind` is "proceed"; leave the defaults otherwise.

Available credentials (the first is the default):
{% for o in options -%}
- **{{ o.name }}** ({{ o.provider }}) — model: {{ o.model_high or "default" }}\
{% if o.model_heavy %}; heavy model: {{ o.model_heavy }}{% endif %}
{% endfor %}
Rules:
- Default: `fixer_llm_credential` = "" and `fixer_llm_tier` = "high". This is \
the right answer for almost every task. Always prefer the default credential.
- Name another credential ONLY when the issue (or a loaded skill, if any) \
explicitly asks for it (e.g. "use the self-hosted model for this repo"). \
Never switch on your own judgment, on the topic of the issue, or because a \
credential sounds more capable or more private.
- Use `fixer_llm_tier` = "heavy" only when the chosen credential lists a heavy \
model AND either a skill or the issue asks for it, or the fix is clearly heavy: \
a large refactor, a change spread across many files or several repos, or a \
subtle concurrency/data-integrity bug. A normal bug fix is "high", even if the \
investigation was long.
- If a skill or the issue asks for a credential or model that is not listed, \
keep the default and explain it in `fixer_llm_reason`.
- Whenever you deviate from the default, give the reason in one short sentence \
in `fixer_llm_reason`, citing the skill or the issue text that asked for it.
=== End fixer model choice ==="""
)


def fixer_llm_block(options: list[LLMOption]) -> str:
    if not options:
        return ""
    return _BLOCK.render(options=options)
