"""Backend half of the third-party plugin contract.

Each case in ``cli/tests/fixtures/thirdparty_plugin_contract.json`` is a set
of repos, a source URL a user connected and the plugin they installed. The
backend must turn that into exactly the spec the case pins; the CLI's half
(``cli/tests/test_thirdparty_plugin_contract.py``) loads the same spec from
the same repos and checks which skills come out the other end.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from api.routers.marketplaces import sources
from api.routers.marketplaces.sources import load_source_manifest, parse_repo_locator
from api.routers.marketplaces.utils import PluginInstallRow, resolve_plugin_specs
from tests.utils.fake_repos import FakeRepos, skill_md

CONTRACT = Path(__file__).parents[3] / "cli/tests/fixtures/thirdparty_plugin_contract.json"
CASES = json.loads(CONTRACT.read_text())["cases"]


def _render(value: object) -> str:
    if isinstance(value, dict) and set(value) == {"skill"}:
        return skill_md(value["skill"])
    return value if isinstance(value, str) else json.dumps(value)


def _fake_repos(case: dict) -> FakeRepos:
    fake = FakeRepos()
    for key, files in case["repos"].items():
        url, _, ref = key.partition("@")
        loc = parse_repo_locator(url)
        fake.add(
            loc.provider,
            loc.project_path,
            {path: _render(v) for path, v in files.items()},
            ref=ref or None,
        )
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_backend_emits_the_contract_spec(case):
    fake = _fake_repos(case)
    stored_url = parse_repo_locator(case["source"]).canonical_url
    sources._described_cache.clear()

    with patch("api.routers.marketplaces.sources.get_current_app", return_value=fake):
        manifest = await load_source_manifest(stored_url, describe=True)
        assert case["install"] in [p.name for p in manifest.plugins]

        specs = await resolve_plugin_specs(
            [
                PluginInstallRow(
                    plugin_name=case["install"],
                    display_name=case["install"],
                    pinned_ref=None,
                    marketplace_git_url=stored_url,
                )
            ]
        )

    assert [s.model_dump(exclude_none=True) for s in specs] == [case["spec"]]


def test_contract_has_a_case_per_source_kind():
    """Guard against the fixture quietly losing coverage."""
    ids = {c["id"] for c in CASES}
    assert {
        "shared-source-skills-allowlist",
        "url-source-pinned-to-a-tag",
        "github-source-with-plugin-json",
        "git-subdir-in-a-monorepo",
        "relative-plugin-folder",
        "bare-name-under-plugin-root",
        "npx-style-skills-repo-one-skill-picked",
        "claude-skills-folder-in-a-gitlab-project",
        "single-skill-repo",
    } <= ids
