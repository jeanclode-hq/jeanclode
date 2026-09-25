"""Evals for the triage agents' fixer LLM choice (#43).

The bar: stay on the default credential and tier unless a skill or the issue
explicitly asks otherwise, escalate to the heavy tier only for clearly heavy
work, and never switch on the topic of an issue alone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.agents.issue.schemas import TriageInput, TriageOutput
from src.agents.issue.triage import TriageAgent
from src.runtime.llm_options import LLMOption, resolve_fixer_llm
from src.skills.schemas import Skill

pytestmark = pytest.mark.eval

REPO = "org/shop"
PROVIDER = "gitlab"

CLAUDE = LLMOption(
    id="c1",
    name="claude",
    provider="claude_code",
    model_high="claude-sonnet-5",
    model_heavy="claude-opus-5-5",
    model_low="claude-haiku-4-5",
)
SELF_HOSTED = LLMOption(
    id="c2",
    name="self-hosted",
    provider="openai_compatible",
    model_high="qwen3-coder",
    model_low="qwen3-small",
    env={"ANTHROPIC_BASE_URL": "https://llm.internal/v1"},
    secret_env={"ANTHROPIC_AUTH_TOKEN": "JEANCLODE_LLM_OPTION_1_SECRET"},
)
OPTIONS = [CLAUDE, SELF_HOSTED]

_CART = """\
def cart_total(cart):
    return sum(item["price"] * item["qty"] for item in cart["items"])


def checkout(cart, payment):
    total = cart_total(cart)
    if cart.get("discount"):
        total -= cart["discount"]["amount"]
    return payment.charge(total)
"""


def _seed_repo(root: Path) -> None:
    (root / "shop").mkdir()
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "checkout.py").write_text(_CART)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _skill(root: Path, name: str, description: str, body: str) -> Skill:
    plugin = root.parent / f"{root.name}-plugin-{name}"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(f'{{"name": "plugin-{name}"}}')
    skill_dir = plugin / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    )
    return Skill(name=name, description=description, skill_dir=skill_dir)


_BUG_BODY = (
    "Checkout crashes with `KeyError: 'amount'` in `shop/checkout.py` `checkout()` when a "
    "discount object has no `amount` (percentage discounts only carry `percent`).\n\n"
    "Steps: apply a 10% discount code, then check out → 500.\n"
    "Expected: the percentage is applied to the total."
)


async def _issue_triage(run_agent, tmp_path: Path, title: str, body: str, **ctx) -> TriageOutput:
    _seed_repo(tmp_path)
    raw = await run_agent(
        TriageAgent,
        TriageInput(
            repo=REPO,
            provider=PROVIDER,
            repo_name=tmp_path.name,
            issue_number="12",
            issue_title=title,
            issue_body=body,
        ),
        llm_options=OPTIONS,
        **ctx,
    )
    return TriageOutput.model_validate(raw or {})


def _resolved(output: TriageOutput) -> tuple[str, str]:
    choice = resolve_fixer_llm(
        OPTIONS, output.fixer_llm_credential, output.fixer_llm_tier, output.fixer_llm_reason
    )
    assert choice.option is not None
    return choice.option.name, choice.tier


# --- defaults ---


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_plain_bug_keeps_the_default(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _issue_triage(
        run_agent, tmp_path, "Checkout 500 on percentage discounts", _BUG_BODY
    )
    assert output.kind == "proceed"
    assert _resolved(output) == ("claude", "high")


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_sensitive_topic_alone_does_not_switch(run_agent, tmp_path, fake_cli_env) -> None:
    body = (
        _BUG_BODY + "\n\nThe checkout payload includes customer emails and card "
        "fingerprints, so be careful with logs."
    )
    output = await _issue_triage(run_agent, tmp_path, "Checkout 500 on percentage discounts", body)
    assert output.kind == "proceed"
    assert _resolved(output) == ("claude", "high")


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_long_investigation_is_not_heavy(run_agent, tmp_path, fake_cli_env) -> None:
    body = (
        _BUG_BODY + "\n\nWe spent two days bisecting this across three services before "
        "finding the line above; the fix itself should be a couple of lines."
    )
    output = await _issue_triage(run_agent, tmp_path, "Checkout 500 on percentage discounts", body)
    assert _resolved(output) == ("claude", "high")


# --- explicit requests ---


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_issue_asking_for_self_hosted_switches(run_agent, tmp_path, fake_cli_env) -> None:
    body = _BUG_BODY + "\n\nPlease run the fix for this one on the `self-hosted` model."
    output = await _issue_triage(run_agent, tmp_path, "Checkout 500 on percentage discounts", body)
    assert output.kind == "proceed"
    assert _resolved(output) == ("self-hosted", "high")
    assert output.fixer_llm_reason


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_issue_asking_for_unknown_model_keeps_default_and_says_so(
    run_agent, tmp_path, fake_cli_env
) -> None:
    body = _BUG_BODY + "\n\nThis must be fixed with our `gpu-cluster` model, not a cloud one."
    output = await _issue_triage(run_agent, tmp_path, "Checkout 500 on percentage discounts", body)
    assert output.kind == "proceed"
    assert _resolved(output)[0] == "claude"
    assert "gpu-cluster" in output.fixer_llm_reason


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_issue_asking_for_the_strongest_model_goes_heavy(
    run_agent, tmp_path, fake_cli_env
) -> None:
    body = _BUG_BODY + "\n\nUse the heavy model for this fix, it's a critical path."
    output = await _issue_triage(run_agent, tmp_path, "Checkout 500 on percentage discounts", body)
    assert output.kind == "proceed"
    assert _resolved(output) == ("claude", "heavy")


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_skill_asking_for_self_hosted_switches(run_agent, tmp_path, fake_cli_env) -> None:
    skill = _skill(
        tmp_path,
        "llm-routing",
        "Which LLM credential the fixer must use for this organisation's repositories.",
        "Every code fix in the `shop` repositories must run on the `self-hosted` LLM "
        "credential. Never use a cloud model for them.",
    )
    output = await _issue_triage(
        run_agent,
        tmp_path,
        "Checkout 500 on percentage discounts",
        _BUG_BODY,
        skills=[skill],
    )
    assert output.kind == "proceed"
    assert _resolved(output) == ("self-hosted", "high")


# --- heavy work ---


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_large_refactor_goes_heavy(run_agent, tmp_path, fake_cli_env) -> None:
    body = (
        "Replace the dict-based cart with a typed `Cart`/`LineItem`/`Discount` model across the "
        "whole codebase: `shop/checkout.py` and every caller, the serializers, the payment "
        "adapters and their tests (~60 files). Percentage and fixed discounts must both be "
        "modelled, and the public checkout API must keep its current JSON shape. "
        "One concern only: the cart data model."
    )
    output = await _issue_triage(
        run_agent, tmp_path, "Refactor the cart to a typed domain model", body
    )
    assert output.kind == "proceed"
    assert _resolved(output) == ("claude", "heavy")
