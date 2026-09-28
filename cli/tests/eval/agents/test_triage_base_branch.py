"""Evals for issue triage's ``base_branch``: set only when someone names the branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.agents.issue.schemas import TriageInput, TriageOutput
from src.agents.issue.triage import TriageAgent

pytestmark = pytest.mark.eval

REPO = "team/site"
PROVIDER = "gitlab"

_SECTION = """\
<template>
  <section class="bg-default text-default">
    <div class="flex w-full flex-col gap-10 px-8 py-12 lg:w-2/3 lg:px-8">
      <slot name="body" />
    </div>
  </section>
</template>
"""

_BODY = """\
Could the **text** be centered on the event pages please?

https://site.example.dev/en/events/post-cookie-era/
https://site.example.dev/fr/conferences/interview-brand-president/

Thanks!
"""


def _seed_repo(root: Path) -> None:
    comp = root / "app" / "components"
    comp.mkdir(parents=True)
    (comp / "InsightSection.vue").write_text(_SECTION)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


async def _triage(run_agent, tmp_path: Path, comments: str = "") -> TriageOutput:
    _seed_repo(tmp_path)
    raw = await run_agent(
        TriageAgent,
        TriageInput(
            repo=REPO,
            provider=PROVIDER,
            repo_name=tmp_path.name,
            issue_number="67",
            issue_title="Page layout",
            issue_body=_BODY,
            comments=comments,
        ),
    )
    return TriageOutput.model_validate(raw or {})


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_branch_named_in_a_comment_is_the_base(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        comments=(
            "alice: @jeanclode-bot ouvre une MR sur `dev` pour corriger ce soucis. "
            "The body sits in `InsightSection.vue`'s `body` slot, which has no centering class."
        ),
    )
    assert output.kind == "proceed"
    assert output.base_branch == "dev"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_no_branch_named_leaves_base_empty(run_agent, tmp_path, fake_cli_env) -> None:
    # The `.dev` hosts in the body are environments, not a branch request.
    output = await _triage(run_agent, tmp_path)
    assert output.kind == "proceed"
    assert output.base_branch == ""
