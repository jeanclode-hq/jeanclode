"""clone_thirdparty_plugins_from_env — the other half of the backend's
JEANCLODE_THIRDPARTY_PLUGINS contract (see src/skills/thirdparty.py)."""

import json
from pathlib import Path
from unittest.mock import patch

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
