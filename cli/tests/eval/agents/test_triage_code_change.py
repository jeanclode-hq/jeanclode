"""Evals for issue triage's ``code_change`` decision.

The bar: an issue whose deliverable is information (a KPI, a count, an
explanation) proceeds with ``code_change=False``; anything that needs a repo
change keeps ``True``, even when it also asks for an answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.agents.issue.schemas import TriageInput, TriageOutput
from src.agents.issue.triage import TriageAgent
from src.runtime.llm_options import LLMOption, resolve_fixer_llm

pytestmark = pytest.mark.eval

REPO = "org/contest"
PROVIDER = "gitlab"

OPTIONS = [
    LLMOption(
        id="c1",
        name="claude",
        provider="claude_code",
        model_high="claude-sonnet-5",
        model_low="claude-haiku-4-5",
    ),
    LLMOption(
        id="c2",
        name="self-hosted",
        provider="openai_compatible",
        model_high="qwen3-coder",
        model_low="qwen3-small",
        env={"ANTHROPIC_BASE_URL": "https://llm.internal/v1"},
        secret_env={"ANTHROPIC_AUTH_TOKEN": "JEANCLODE_LLM_OPTION_1_SECRET"},
    ),
]

_MODELS = """\
from sqlalchemy import Column, DateTime, Integer, String

from contest.db import Base


class Registration(Base):
    __tablename__ = "registrations"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False)
    ip = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)
"""

_SIGNUP = """\
from contest.models import Registration


def register(session, email, ip, now):
    session.add(Registration(email=email.strip(), ip=ip, created_at=now))
    session.commit()


def daily_limit_reached(session, ip, now):
    count = session.query(Registration).filter(Registration.ip == ip).count()
    return count > 5
"""


def _seed_repo(root: Path) -> None:
    pkg = root / "contest"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "db.py").write_text(
        "from sqlalchemy.orm import declarative_base\n\nBase = declarative_base()\n"
    )
    (pkg / "models.py").write_text(_MODELS)
    (pkg / "signup.py").write_text(_SIGNUP)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


async def _triage(run_agent, tmp_path: Path, title: str, body: str) -> TriageOutput:
    _seed_repo(tmp_path)
    raw = await run_agent(
        TriageAgent,
        TriageInput(
            repo=REPO,
            provider=PROVIDER,
            repo_name=tmp_path.name,
            issue_number="129",
            issue_title=title,
            issue_body=body,
        ),
        llm_options=OPTIONS,
    )
    return TriageOutput.model_validate(raw or {})


_KPI_BODY = """\
<!--
  The deliverable is the KPI itself, returned as a comment on this issue.
  Compute it from the project's database and post the result in a comment.
  Answer directly in this issue as a comment. NEVER create a MR/PR.
  Set the model to `self-hosted`.
-->

## KPI definition

Table of the 30 IPs with the most registrations, sorted by registration count
(highest first).

## Output

- [ ] Simple data
- [ ] Generated graph
- [x] Table
"""


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_kpi_request_is_an_answer_on_the_named_model(
    run_agent, tmp_path, fake_cli_env
) -> None:
    output = await _triage(run_agent, tmp_path, "Custom KPI - registration count by IP", _KPI_BODY)
    assert output.kind == "proceed"
    assert output.code_change is False
    choice = resolve_fixer_llm(
        OPTIONS, output.fixer_llm_credential, output.fixer_llm_tier, output.fixer_llm_reason
    )
    assert choice.option is not None
    assert choice.option.name == "self-hosted"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_bug_is_a_code_change(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Daily registration limit counts every day, not just today",
        "`daily_limit_reached` in `contest/signup.py` counts all registrations ever made "
        "from an IP, so anyone who registered 6 times last month is blocked forever. "
        "It should only count registrations from the current day (`now`).",
    )
    assert output.kind == "proceed"
    assert output.code_change is True


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_fix_that_also_wants_an_answer_is_a_code_change(
    run_agent, tmp_path, fake_cli_env
) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        "Emails stored with mixed case, and how many duplicates do we have?",
        "`register` in `contest/signup.py` stores the email as typed, so `Bob@x.com` and "
        "`bob@x.com` count as two registrations. Please lowercase emails on registration, "
        "and in a comment tell us how many registrations are case-insensitive duplicates "
        "today.",
    )
    assert output.kind == "proceed"
    assert output.code_change is True
