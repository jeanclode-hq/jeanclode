"""clone_thirdparty_plugins_from_env — the other half of the backend's
JEANCLODE_THIRDPARTY_PLUGINS contract (see src/skills/thirdparty.py)."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.skills.discovery import load_skills
from src.skills.thirdparty import clone_thirdparty_plugins_from_env


def _make_plugin_dir(root: Path, subpath: str | None = None) -> Path:
    """Build a fake clone containing a Claude Code plugin, optionally nested."""
    plugin_root = root / subpath if subpath else root
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text('{"name": "p"}')
    return root


def _env(specs: list[dict], *, enabled: bool = True) -> dict[str, str]:
    env = {"JEANCLODE_THIRDPARTY_PLUGINS": json.dumps(specs)}
    if enabled:
        env["JEANCLODE_THIRDPARTY_PLUGINS_ENABLED"] = "1"
    return env


def test_returns_empty_when_not_enabled(tmp_path: Path) -> None:
    env = _env([{"git_url": "https://example.com/org/repo"}], enabled=False)
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


def test_returns_empty_when_unset(tmp_path: Path) -> None:
    assert clone_thirdparty_plugins_from_env(tmp_path, env={}) == []


def test_returns_empty_on_malformed_json(tmp_path: Path) -> None:
    env = {"JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1", "JEANCLODE_THIRDPARTY_PLUGINS": "not json"}
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_clones_each_spec_and_returns_plugin_root(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/org/handbook", "ref": "v1"}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert roots == [clone_dir]
    mock_clone.assert_called_once()
    assert mock_clone.call_args.args[0] == "https://example.com/org/handbook"
    assert mock_clone.call_args.kwargs.get("ref") == "v1"


@patch("src.skills.thirdparty.clone_repo")
def test_resolves_plugin_subpath_within_the_clone(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir, subpath="skills-monorepo/handbook")
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://example.com/org/monorepo",
                "plugin_subpath": "skills-monorepo/handbook",
            }
        ]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert roots == [clone_dir / "skills-monorepo" / "handbook"]


@patch("src.skills.thirdparty.clone_repo")
def test_each_spec_clones_into_its_own_subdir(mock_clone, tmp_path: Path) -> None:
    """Two installs off the same marketplace repo at different refs must
    not collide on the same clone target directory."""
    mock_clone.side_effect = lambda _url, workspace, ref=None: _make_plugin_dir(workspace / "repo")

    env = _env(
        [
            {"git_url": "https://example.com/org/marketplace", "ref": "v1"},
            {"git_url": "https://example.com/org/marketplace", "ref": "v2"},
        ]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert len(roots) == 2
    assert roots[0] != roots[1]
    call_targets = [c.args[1] for c in mock_clone.call_args_list]
    assert len(set(call_targets)) == 2


@patch("src.skills.thirdparty.clone_repo")
def test_skips_spec_with_no_git_url(mock_clone, tmp_path: Path) -> None:
    env = _env([{"ref": "v1"}])
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []
    mock_clone.assert_not_called()


@patch("src.skills.thirdparty.clone_repo")
def test_clone_failure_is_skipped_not_fatal(mock_clone, tmp_path: Path) -> None:
    mock_clone.side_effect = RuntimeError("clone failed")
    env = _env([{"git_url": "https://example.com/org/broken"}])
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_skips_clone_missing_plugin_manifest(mock_clone, tmp_path: Path) -> None:
    """A restructured marketplace repo (bad plugin_subpath) shouldn't crash the run."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/org/repo", "plugin_subpath": "moved"}])
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


def _make_skill_dir(clone_dir: Path, relpath: str) -> Path:
    skill_dir = clone_dir / relpath
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nbody\n")
    return skill_dir


@patch("src.skills.thirdparty.clone_repo")
def test_skills_allowlist_shims_a_plugin_root_when_no_manifest(mock_clone, tmp_path: Path) -> None:
    """Marketplace layout where several plugins share one source root and are
    told apart only by a ``skills`` allowlist (no dedicated plugin.json) —
    the exact shape that silently dropped every plugin before this fix."""
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/handbook")
    _make_skill_dir(clone_dir, "skills/helm")  # sibling plugin's skill, not listed below
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://example.com/org/skills",
                "display_name": "handbook",
                "skills": ["./skills/handbook"],
            }
        ]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert len(roots) == 1
    shim_root = roots[0]
    assert (shim_root / ".claude-plugin" / "plugin.json").is_file()
    assert (shim_root / "skills" / "handbook").resolve() == (clone_dir / "skills" / "handbook")
    assert not (shim_root / "skills" / "helm").exists()


@patch("src.skills.thirdparty.clone_repo")
def test_skills_allowlist_entry_missing_skill_md_is_skipped(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    (clone_dir / "skills" / "ghost").mkdir(parents=True)
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://example.com/org/skills",
                "skills": ["./skills/ghost"],
            }
        ]
    )
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_plugin_json_takes_priority_over_skills_allowlist(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/org/repo", "skills": ["./skills/unused"]}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert roots == [clone_dir]


@patch("src.skills.thirdparty.clone_repo")
def test_skills_allowlist_resolves_from_the_plugin_subpath(mock_clone, tmp_path: Path) -> None:
    """Claude Code resolves an entry's ``skills`` from the plugin root, not the repo root."""
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "tools/fmt/skills/fmt")
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://example.com/org/mono",
                "plugin_subpath": "tools/fmt",
                "skills": ["./skills/fmt"],
            }
        ]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert (roots[0] / "skills" / "fmt").resolve() == (clone_dir / "tools/fmt/skills/fmt").resolve()


@patch("src.skills.thirdparty.clone_repo")
def test_skills_outside_the_plugin_root_are_refused(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    _make_skill_dir(tmp_path, "elsewhere")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/org/x", "skills": ["../elsewhere"]}])
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.subprocess.run")
@patch("src.skills.thirdparty.clone_repo")
def test_sha_pin_clones_default_branch_then_checks_out_the_commit(
    mock_clone, mock_run, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir
    sha = "a" * 40

    env = _env([{"git_url": "https://example.com/org/p", "ref": "v1", "sha": sha}])
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == [clone_dir]

    assert mock_clone.call_args.kwargs["ref"] is None
    fetch, checkout = (c.args[0] for c in mock_run.call_args_list)
    assert fetch[-2:] == ["origin", sha]
    assert checkout[-1] == "FETCH_HEAD"


def _skill_names(roots: list[Path]) -> list[str]:
    """Skill folder names loaded (``_make_skill_dir`` names every skill ``s``)."""
    return sorted(s.skill_dir.name for s in load_skills(roots))


def _clone_into(clone_dir: Path):
    """A ``clone_repo`` stand-in that always yields ``clone_dir``."""
    return lambda _url, _workspace, ref=None: clone_dir


@patch("src.skills.thirdparty.clone_repo")
def test_one_broken_spec_never_costs_the_others(mock_clone, tmp_path: Path) -> None:
    good = tmp_path / "good"
    _make_skill_dir(good, "skills/ok")

    def clone(url, workspace, ref=None):
        if "broken" in url:
            raise RuntimeError("clone failed")
        return good

    mock_clone.side_effect = clone
    env = _env(
        [
            {"git_url": "https://example.com/org/broken", "skills": ["./skills/ok"]},
            "not a dict",
            {"no": "git_url"},
            {"git_url": "https://example.com/org/good", "skills": ["./skills/ok"]},
        ]
    )

    assert _skill_names(clone_thirdparty_plugins_from_env(tmp_path, env=env)) == ["ok"]


def test_non_array_payload_is_ignored(tmp_path: Path) -> None:
    env = {"JEANCLODE_THIRDPARTY_PLUGINS_ENABLED": "1", "JEANCLODE_THIRDPARTY_PLUGINS": "{}"}
    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.subprocess.run")
@patch("src.skills.thirdparty.clone_repo")
def test_unreachable_sha_skips_the_spec(mock_clone, mock_run, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir
    mock_run.side_effect = subprocess.CalledProcessError(128, ["git", "fetch"])

    env = _env([{"git_url": "https://example.com/org/p", "sha": "c" * 40}])

    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_ref_is_passed_to_clone_when_there_is_no_sha(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir

    clone_thirdparty_plugins_from_env(
        tmp_path, env=_env([{"git_url": "https://example.com/org/p", "ref": "6.47.1"}])
    )

    assert mock_clone.call_args.kwargs["ref"] == "6.47.1"


@patch("src.skills.thirdparty.clone_repo")
def test_no_ref_tracks_the_default_branch(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    mock_clone.return_value = clone_dir

    clone_thirdparty_plugins_from_env(tmp_path, env=_env([{"git_url": "https://example.com/o/p"}]))

    assert mock_clone.call_args.kwargs["ref"] is None


@pytest.mark.parametrize("subpath", ["../outside", "a/../../outside", "/etc"])
@patch("src.skills.thirdparty.clone_repo")
def test_plugin_subpath_cannot_leave_the_clone(mock_clone, subpath, tmp_path: Path) -> None:
    clone_dir = tmp_path / "work" / "clone"
    clone_dir.mkdir(parents=True)
    _make_plugin_dir(tmp_path / "work" / "outside")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "plugin_subpath": subpath}])

    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_symlinked_skill_folder_escaping_the_repo_is_refused(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    (clone_dir / "skills").mkdir(parents=True)
    secret = _make_skill_dir(tmp_path, "host-secrets")
    (clone_dir / "skills" / "evil").symlink_to(secret, target_is_directory=True)
    _make_skill_dir(clone_dir, "skills/fine")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "skills": ["./skills"]}])

    assert _skill_names(clone_thirdparty_plugins_from_env(tmp_path, env=env)) == ["fine"]


@patch("src.skills.thirdparty.clone_repo")
def test_skill_md_symlink_escaping_the_repo_is_refused(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    (clone_dir / "skills" / "evil").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: evil\ndescription: d\n---\n")
    (clone_dir / "skills" / "evil" / "SKILL.md").symlink_to(outside)
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "skills": ["./skills/evil"]}])

    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_skills_entry_naming_a_folder_of_skills_loads_each(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "custom/a")
    _make_skill_dir(clone_dir, "custom/b")
    (clone_dir / "custom" / "notes").mkdir()
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "skills": "./custom"}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["a", "b"]


@patch("src.skills.thirdparty.clone_repo")
def test_skill_folders_sharing_a_name_both_load(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/review")
    _make_skill_dir(clone_dir, "legacy/review")
    mock_clone.return_value = clone_dir

    env = _env(
        [{"git_url": "https://example.com/o/p", "skills": ["./skills/review", "./legacy/review"]}]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["review", "review-2"]
    assert len(load_skills(roots)) == 2


@patch("src.skills.thirdparty.clone_repo")
def test_the_same_skill_listed_twice_loads_once(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/a")
    mock_clone.return_value = clone_dir

    env = _env(
        [{"git_url": "https://example.com/o/p", "skills": ["./skills/a", "skills/a/", "./skills"]}]
    )

    assert len(load_skills(clone_thirdparty_plugins_from_env(tmp_path, env=env))) == 1


@pytest.mark.parametrize("display_name", ["commit-style", "legacy/review", None])
@patch("src.skills.thirdparty.clone_repo")
def test_repo_root_skill_links_under_the_plugin_name(
    mock_clone, display_name, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "acme_commit-style"
    clone_dir.mkdir()
    (clone_dir / "SKILL.md").write_text("---\ndescription: d\n---\nbody\n")
    mock_clone.return_value = clone_dir

    spec = {"git_url": "https://example.com/acme/commit-style", "skills": ["."]}
    if display_name:
        spec["display_name"] = display_name
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=_env([spec]))

    [link] = list((roots[0] / "skills").iterdir())
    assert "/" not in link.name
    assert link.resolve() == clone_dir.resolve()
    [skill] = load_skills(roots)
    assert skill.description == "d"


@patch("src.skills.thirdparty.clone_repo")
def test_plugin_json_skills_field_adds_to_the_default_folder(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    (clone_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "kit", "skills": ["./extra/one", "./extra/missing"]})
    )
    _make_skill_dir(clone_dir, "skills/base")
    _make_skill_dir(clone_dir, "extra/one")
    mock_clone.return_value = clone_dir

    roots = clone_thirdparty_plugins_from_env(
        tmp_path, env=_env([{"git_url": "https://example.com/o/kit"}])
    )

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["base", "one"]


@patch("src.skills.thirdparty.clone_repo")
def test_entry_and_plugin_json_skills_are_merged(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    (clone_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "kit", "skills": "./extra/a"})
    )
    _make_skill_dir(clone_dir, "extra/a")
    _make_skill_dir(clone_dir, "extra/b")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/kit", "skills": ["./extra/b"]}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["a", "b"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
@patch("src.skills.thirdparty.clone_repo")
def test_unreadable_plugin_json_still_loads_the_plugin_folder(
    mock_clone, content, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    (clone_dir / ".claude-plugin" / "plugin.json").write_text(content)
    mock_clone.return_value = clone_dir

    roots = clone_thirdparty_plugins_from_env(
        tmp_path, env=_env([{"git_url": "https://example.com/o/p"}])
    )

    assert roots == [clone_dir]


@patch("src.skills.thirdparty.clone_repo")
def test_repo_without_plugin_json_or_skills_list_uses_its_skills_folder(
    mock_clone, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/one")
    _make_skill_dir(clone_dir, "skills/two")
    _make_skill_dir(clone_dir, "elsewhere/three")
    mock_clone.return_value = clone_dir

    roots = clone_thirdparty_plugins_from_env(
        tmp_path, env=_env([{"git_url": "https://example.com/o/p"}])
    )

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["one", "two"]


@pytest.mark.parametrize("skills", [[], [""], [None, 3], "   "])
@patch("src.skills.thirdparty.clone_repo")
def test_empty_or_junk_skills_list_falls_back_to_skills_folder(
    mock_clone, skills, tmp_path: Path
) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/one")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "skills": skills}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert [p.name for p in (roots[0] / "skills").iterdir()] == ["one"]


@patch("src.skills.thirdparty.clone_repo")
def test_skills_resolve_inside_the_plugin_subpath_but_may_reach_its_repo(
    mock_clone, tmp_path: Path
) -> None:
    """Paths are relative to the plugin root; ``..`` within the same repo is allowed."""
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "packages/docs/skills/write")
    _make_skill_dir(clone_dir, "shared/style")
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://example.com/o/mono",
                "plugin_subpath": "packages/docs",
                "skills": ["./skills/write", "../../shared/style"],
            }
        ]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert sorted(p.name for p in (roots[0] / "skills").iterdir()) == ["style", "write"]


@patch("src.skills.thirdparty.clone_repo")
def test_shim_failure_is_contained(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_skill_dir(clone_dir, "skills/a")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p", "skills": ["./skills/a"]}])
    with patch("pathlib.Path.symlink_to", side_effect=OSError("read-only fs")):
        assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []


@patch("src.skills.thirdparty.clone_repo")
def test_skills_from_one_repo_share_a_single_clone(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    for name in ("vue", "pinia", "vite"):
        _make_skill_dir(clone_dir, f"skills/{name}")
    mock_clone.return_value = clone_dir

    env = _env(
        [
            {
                "git_url": "https://github.com/antfu/skills",
                "display_name": n,
                "skills": [f"./skills/{n}"],
            }
            for n in ("vue", "pinia")
        ]
        + [{"git_url": "https://github.com/antfu/skills.git", "skills": ["./skills/vite"]}]
    )
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    mock_clone.assert_called_once()
    assert _skill_names(roots) == ["pinia", "vite", "vue"]


@patch("src.skills.thirdparty.clone_repo")
def test_different_ref_or_sha_gets_its_own_clone(mock_clone, tmp_path: Path) -> None:
    mock_clone.side_effect = lambda _url, workspace, ref=None: (
        _make_skill_dir(workspace / "repo", "skills/a").parent.parent
    )
    url = "https://example.com/o/p"

    with patch("src.skills.thirdparty.subprocess.run"):
        roots = clone_thirdparty_plugins_from_env(
            tmp_path,
            env=_env(
                [
                    {"git_url": url, "skills": ["./skills/a"]},
                    {"git_url": url, "ref": "v1", "skills": ["./skills/a"]},
                    {"git_url": url, "sha": "a" * 40, "skills": ["./skills/a"]},
                    {"git_url": url, "sha": "b" * 40, "skills": ["./skills/a"]},
                    {"git_url": url, "ref": "v1", "skills": ["./skills/a"]},
                ]
            ),
        )

    assert mock_clone.call_count == 4
    assert len(roots) == 5


@patch("src.skills.thirdparty.clone_repo")
def test_a_failed_clone_is_not_retried_for_every_skill(mock_clone, tmp_path: Path) -> None:
    mock_clone.side_effect = RuntimeError("clone failed")
    env = _env([{"git_url": "https://example.com/o/p", "skills": [f"./skills/{n}"]} for n in "abc"])

    assert clone_thirdparty_plugins_from_env(tmp_path, env=env) == []
    mock_clone.assert_called_once()


@patch("src.skills.thirdparty.clone_repo")
def test_two_plugins_resolving_to_one_folder_load_it_once(mock_clone, tmp_path: Path) -> None:
    clone_dir = tmp_path / "clone"
    _make_plugin_dir(clone_dir)
    _make_skill_dir(clone_dir, "skills/a")
    mock_clone.return_value = clone_dir

    env = _env([{"git_url": "https://example.com/o/p"}, {"git_url": "https://example.com/o/p"}])
    roots = clone_thirdparty_plugins_from_env(tmp_path, env=env)

    assert roots == [clone_dir]
    assert len(load_skills(roots)) == 1
