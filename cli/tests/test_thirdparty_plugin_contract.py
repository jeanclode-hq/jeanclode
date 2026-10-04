"""CLI half of the third-party plugin contract (see tests/fixtures/thirdparty_plugin_contract.json).

The backend half pins the spec each case resolves to; here that exact spec is
cloned from the same repos — ``clone_repo`` writes them into ``tmp_path``,
nothing touches the network — and the skills it loads are checked.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.skills.discovery import load_skills
from src.skills.thirdparty import clone_thirdparty_plugins_from_env

CONTRACT = Path(__file__).parent / "fixtures" / "thirdparty_plugin_contract.json"
CASES = json.loads(CONTRACT.read_text())["cases"]


def _render(value: object) -> str:
    if isinstance(value, dict) and set(value) == {"skill"}:
        return f"---\nname: {value['skill']}\ndescription: does things\n---\nbody\n"
    return value if isinstance(value, str) else json.dumps(value)


def _fake_clone(repos: dict[str, dict]):
    """A ``clone_repo`` that writes the case's repo at the requested ref."""

    def clone(git_url: str, workspace: Path, token: str = "", *, ref: str | None = None) -> Path:
        url = git_url.removesuffix(".git").rstrip("/")
        files = repos.get(f"{url}@{ref}" if ref else url)
        if files is None:
            raise RuntimeError(f"clone failed: {git_url} has no ref {ref!r}")
        target = workspace / "repo"
        for rel, value in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render(value))
        return target

    return clone


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_cli_loads_the_contract_skills(case, tmp_path: Path) -> None:
    env = {
        "JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1",
        "JEANCLODE_THIRDPARTY_PLUGINS": json.dumps([case["spec"]]),
    }
    with patch("src.skills.thirdparty.clone_repo", side_effect=_fake_clone(case["repos"])):
        roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert sorted(s.name for s in load_skills(roots)) == sorted(case["skills"])


def test_every_contract_case_in_one_payload_loads_independently(tmp_path: Path) -> None:
    """The backend sends every install in one array; each spec clones on its own."""
    clones = [_fake_clone(c["repos"]) for c in CASES]

    def clone(git_url: str, workspace: Path, token: str = "", *, ref: str | None = None) -> Path:
        # Each spec clones into ``.thirdparty-plugins/<its index>``.
        return clones[int(workspace.name)](git_url, workspace, token, ref=ref)

    env = {
        "JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1",
        "JEANCLODE_THIRDPARTY_PLUGINS": json.dumps([c["spec"] for c in CASES]),
    }
    with patch("src.skills.thirdparty.clone_repo", side_effect=clone):
        roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert len(roots) == len(CASES)
    expected = sorted(name for c in CASES for name in c["skills"])
    assert sorted(s.name for s in load_skills(roots)) == expected
