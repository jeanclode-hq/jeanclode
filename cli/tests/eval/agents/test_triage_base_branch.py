"""Evals for issue triage's ``base_branch``.

Each case backs the clone with a real bare ``origin`` holding the listed
branches, cloned shallow and single-branch like production, so triage has to
ask the remote to see them — however it chooses to.
"""

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

_HINT = "The body sits in `InsightSection.vue`'s `body` slot, which has no centering class."

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=cwd, check=True, capture_output=True)


def _seed_origin(
    root: Path, branches: list[str], default: str, extra_files: dict[str, str]
) -> Path:
    seed = root.parent / f"{root.name}-seed"
    origin = root.parent / f"{root.name}-origin.git"
    comp = seed / "app" / "components"
    comp.mkdir(parents=True)
    (comp / "InsightSection.vue").write_text(_SECTION)
    for name, content in extra_files.items():
        (seed / name).write_text(content)
    _git(seed, "init", "-q", "-b", default)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    for branch in branches:
        if branch == default:
            continue
        _git(seed, "checkout", "-q", "-b", branch, default)
        (seed / "CHANGELOG.md").write_text(f"work in progress on {branch}\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"wip on {branch}")
    _git(root.parent, "init", "-q", "--bare", str(origin))
    _git(seed, "push", "-q", str(origin), "--all")
    _git(origin, "symbolic-ref", "HEAD", f"refs/heads/{default}")
    return origin


def _clone(root: Path, origin: Path, default: str) -> None:
    refspec = f"+refs/heads/{default}:refs/remotes/origin/{default}"
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "config", "remote.origin.fetch", refspec)
    _git(root, "fetch", "-q", "--depth", "1", "origin")
    _git(root, "checkout", "-q", "-b", default, f"origin/{default}")
    _git(root, "remote", "set-head", "origin", default)


async def _triage(
    run_agent,
    tmp_path: Path,
    *,
    branches: list[str],
    comments: str = "",
    default: str = "main",
    extra_files: dict[str, str] | None = None,
) -> TriageOutput:
    origin = _seed_origin(tmp_path, branches, default, extra_files or {})
    _clone(tmp_path, origin, default)
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
        branches=["main", "dev"],
        comments=f"alice: @jeanclode-bot ouvre une MR sur `dev` pour corriger ce soucis. {_HINT}",
    )
    assert output.kind == "proceed"
    assert output.base_branch == "dev"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_loose_name_resolves_to_the_existing_branch(
    run_agent, tmp_path, fake_cli_env
) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        branches=["main", "develop"],
        comments=f"alice: @jeanclode-bot please fix this and open the MR on the dev branch. {_HINT}",
    )
    assert output.kind == "proceed"
    assert output.base_branch == "develop"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_exact_match_wins_over_a_near_one(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        branches=["main", "dev", "develop"],
        comments=f"alice: @jeanclode-bot please open the MR on `dev`. {_HINT}",
    )
    assert output.kind == "proceed"
    assert output.base_branch == "dev"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_repo_convention_sets_the_base(run_agent, tmp_path, fake_cli_env) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        branches=["main", "develop"],
        comments=f"alice: @jeanclode-bot can you fix this? {_HINT}",
        extra_files={
            "CONTRIBUTING.md": (
                "# Contributing\n\n## Branching\n\n"
                "All merge requests target `develop`. `main` only receives release merges.\n"
            )
        },
    )
    assert output.kind == "proceed"
    assert output.base_branch == "develop"


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_unknown_branch_asks_instead_of_falling_back(
    run_agent, tmp_path, fake_cli_env
) -> None:
    output = await _triage(
        run_agent,
        tmp_path,
        branches=["main", "develop"],
        comments=f"alice: @jeanclode-bot open the MR on `preprod` please. {_HINT}",
    )
    assert output.kind == "needs_info"
    assert "develop" in output.comment_body


@pytest.mark.parametrize("fake_cli_env", [{}], indirect=True)
async def test_no_branch_named_leaves_base_empty(run_agent, tmp_path, fake_cli_env) -> None:
    # The `.dev` hosts in the body are environments, and a `dev` branch exists
    # to tempt the mapping — still nobody asked for it.
    output = await _triage(
        run_agent,
        tmp_path,
        branches=["main", "dev"],
        comments=f"alice: @jeanclode-bot can you fix this? {_HINT}",
    )
    assert output.kind == "proceed"
    assert output.base_branch in ("", "main")
