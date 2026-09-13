"""Discovery walks Claude Code plugin folders for skills."""

from pathlib import Path

from src.skills.discovery import load_skills, load_skills_from_env


def _make_plugin(root: Path, name: str, *, skills: dict[str, str]) -> Path:
    """Create a minimal Claude Code plugin folder under ``root``.

    ``skills`` maps skill_name -> description.
    """
    plugin = root / name
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(f'{{"name": "{name}"}}')
    skills_dir = plugin / "skills"
    skills_dir.mkdir()
    for skill_name, desc in skills.items():
        sd = skills_dir / skill_name
        sd.mkdir()
        (sd / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: {desc}\n---\nbody")
    return plugin


def test_load_skills_walks_plugin_skills_dir(tmp_path: Path) -> None:
    plugin = _make_plugin(
        tmp_path,
        "user-skills",
        skills={"ruff": "ruff style", "sec": "security"},
    )

    skills = load_skills([plugin])

    assert {s.name for s in skills} == {"ruff", "sec"}
    for s in skills:
        assert s.plugin_path == plugin
        assert s.skill_dir == plugin / "skills" / s.name


def test_load_skills_skips_path_that_is_not_a_plugin(tmp_path: Path) -> None:
    not_a_plugin = tmp_path / "loose"
    not_a_plugin.mkdir()
    skills = load_skills([not_a_plugin])
    assert skills == []


def test_load_skills_skips_plugin_with_no_skills_dir(tmp_path: Path) -> None:
    plugin = tmp_path / "empty"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "empty"}')
    skills = load_skills([plugin])
    assert skills == []


def test_load_skills_skips_skills_missing_frontmatter(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, "p", skills={"good": "good one"})
    bad = plugin / "skills" / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter here")

    skills = load_skills([plugin])

    assert [s.name for s in skills] == ["good"]


def test_load_skills_skips_skills_missing_description(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, "p", skills={"ok": "fine"})
    incomplete = plugin / "skills" / "incomplete"
    incomplete.mkdir()
    (incomplete / "SKILL.md").write_text("---\nname: incomplete\n---\nbody")

    skills = load_skills([plugin])

    assert [s.name for s in skills] == ["ok"]


def test_load_skills_falls_back_to_skill_dir_name(tmp_path: Path) -> None:
    plugin = tmp_path / "p"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "p"}')
    sd = plugin / "skills" / "auto-named"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\ndescription: from filename\n---\nbody")

    [skill] = load_skills([plugin])
    assert skill.name == "auto-named"


def test_load_skills_from_env_parses_colon_separated_paths(tmp_path: Path) -> None:
    p1 = _make_plugin(tmp_path, "p1", skills={"a": "A"})
    p2 = _make_plugin(tmp_path, "p2", skills={"b": "B"})

    env = {"JEANCLODE_SKILLS": f"{p1}:{p2}"}
    skills = load_skills_from_env(env=env)

    assert {s.name for s in skills} == {"a", "b"}


def test_load_skills_from_env_returns_empty_when_unset() -> None:
    assert load_skills_from_env(env={}) == []
    assert load_skills_from_env(env={"JEANCLODE_SKILLS": ""}) == []
