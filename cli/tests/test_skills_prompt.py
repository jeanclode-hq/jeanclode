"""Discovery block format."""

from pathlib import Path

from src.skills.prompt import (
    continuity_protocol_block,
    discovery_block,
    memory_protocol_block,
    related_repos_block,
)
from src.skills.schemas import Skill


def _skill(name: str, description: str) -> Skill:
    return Skill(name=name, description=description, skill_dir=Path(f"/x/{name}"))


def test_empty_skills_yields_empty_block() -> None:
    assert discovery_block([]) == ""


def test_block_lists_each_skill_by_name_and_description() -> None:
    block = discovery_block([_skill("ruff", "ruff style"), _skill("sec", "security")])
    assert "**ruff** — ruff style" in block
    assert "**sec** — security" in block


def test_block_mentions_the_skill_tool_for_invocation() -> None:
    block = discovery_block([_skill("a", "A")])
    assert 'Skill(name="<name>")' in block


def test_block_includes_attribution_and_guardrail_reminder() -> None:
    block = discovery_block([_skill("a", "A")])
    assert "Per <skill-name> skill:" in block
    assert "guardrail" in block.lower()


def test_block_is_delimited_by_header_and_footer() -> None:
    block = discovery_block([_skill("a", "A")])
    assert block.startswith("=== User-loaded skills ===")
    assert block.endswith("=== End user-loaded skills ===")


def test_empty_related_repos_yields_empty_block() -> None:
    assert related_repos_block([]) == ""


def test_related_repos_block_lists_each_repo_by_name_and_path() -> None:
    block = related_repos_block([{"name": "shared-lib", "path": "/tmp/ws/shared-lib"}])
    assert "**shared-lib** — /tmp/ws/shared-lib" in block


def test_related_repos_block_is_delimited_by_header_and_footer() -> None:
    block = related_repos_block([{"name": "a", "path": "/x/a"}])
    assert block.startswith("=== Related repositories ===")
    assert block.endswith("=== End related repositories ===")


def test_related_repos_block_warns_against_assuming_shared_tooling() -> None:
    block = related_repos_block([{"name": "a", "path": "/x/a"}])
    assert "separate git repo" in block


def test_memory_protocol_block_is_delimited_by_header_and_footer() -> None:
    block = memory_protocol_block()
    assert block.startswith("=== Memory ===")
    assert block.endswith("=== End memory ===")


def test_memory_protocol_block_leads_with_view_first_like_the_reference_tool() -> None:
    """Matches the emphatic lead instruction Anthropic's own auto-injected
    memory_20250818 system prompt uses ("ALWAYS ... BEFORE DOING ANYTHING
    ELSE") — proven-effective phrasing worth keeping even though everything
    after it deliberately diverges from the reference framing."""
    block = memory_protocol_block()
    first_paragraph = block.split("\n\n")[0]
    assert "ALWAYS VIEW YOUR MEMORY DIRECTORY FIRST" in first_paragraph
    assert "BEFORE DOING ANYTHING ELSE" in first_paragraph


def test_memory_protocol_block_frames_memory_as_durable_not_scratch_state() -> None:
    """The opposite of the reference tool's framing, deliberately: our
    memory is cross-run, cross-workspace institutional knowledge, not a
    progress log for surviving a context reset within one run."""
    block = memory_protocol_block()
    assert "durable" in block
    assert "cross-run" in block
    assert "scratch space" in block
    assert "A log of this run" in block


def test_memory_protocol_block_steers_sentry_verdicts_into_one_file_per_repo() -> None:
    block = memory_protocol_block()
    assert "sentry-known-noise.md" in block
    assert "not one file per Sentry issue" in block
    assert "_workspace/" in block


def test_continuity_protocol_block_is_delimited_by_header_and_footer() -> None:
    block = continuity_protocol_block()
    assert block.startswith("=== Continuity ===")
    assert block.endswith("=== End continuity ===")


def test_continuity_protocol_block_warns_of_prior_jeanclode_turns_in_thread() -> None:
    block = continuity_protocol_block()
    assert "other jeanclode agents" in block
    assert "before you" in block


def test_continuity_protocol_block_tells_agent_not_to_redo_settled_points() -> None:
    block = continuity_protocol_block()
    assert "treat it as settled" in block
    assert "don't" in block.lower()
