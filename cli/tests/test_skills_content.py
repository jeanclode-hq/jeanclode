from __future__ import annotations

from pathlib import Path

from src.skills.content import find_skill_by_name, read_skill_instructions
from src.skills.discovery import load_skills


def test_read_skill_instructions_strips_frontmatter(tmp_path: Path) -> None:
    plugin = tmp_path / "p"
    skill_dir = plugin / "skills" / "vue3-review"
    skill_dir.mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: vue3-review\ndescription: Vue review\n---\n# Checklist\n"
    )
    skills = load_skills([plugin])
    assert find_skill_by_name(skills, "vue3-review") is not None
    body = read_skill_instructions(skills[0])
    assert body.startswith("# Checklist")
    assert "---" not in body.splitlines()[0]
