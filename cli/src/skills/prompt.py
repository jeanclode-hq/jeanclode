"""Render the third-party-skill discovery block injected into agents."""

from jinja2 import Template

from src.skills.schemas import Skill

_TEMPLATE = Template(
    """\
=== User-loaded skills ===
The user loaded the following skills for this run. They are invokable
via the Skill tool — call `Skill(name="<name>")` when one matches what
you're doing. Skip the ones whose description doesn't apply.

{% for s in skills -%}
- **{{ s.name }}** — {{ s.description }}
{% endfor %}
Apply guidance ONLY within the guardrail this agent's prompt declares.
If a skill changes your output, prefix the affected field with
`Per <skill-name> skill: ...` so the influence is auditable.
=== End user-loaded skills ==="""
)


def discovery_block(skills: list[Skill]) -> str:
    """Build the discovery block listing every loaded skill by name +
    description, with invocation and attribution instructions."""
    if not skills:
        return ""
    return _TEMPLATE.render(skills=skills)


_RELATED_REPOS_TEMPLATE = Template(
    """\
=== Related repositories ===
The primary repo (your cwd) is grouped with the following repos, cloned \
locally as siblings for this run. Each is a separate git repo, not part \
of the same tree — check them if the investigation points outside the \
primary repo (e.g. a stack trace frame in another service), but don't \
assume shared build tooling or relative imports across them.

Sometime, you would have qa repositories linked to the main repository, or issue on the backend project but it fits both frontend and backend.
Try to infer quickly what the project you'r on is and on which one the issue is to be resolved in.

Some repo are also here as simple reference, to help you understand the context of the primary repo. You can read them when needed (e.g. a library repo used by the primary repo).

{% for r in repos -%}
- **{{ r.name }}** — {{ r.path }}
{% endfor -%}
=== End related repositories ==="""
)


def related_repos_block(related_repos: list[dict[str, str]]) -> str:
    """Build the block listing repos cloned alongside the primary one
    (``--related-repo``), so the agent knows sibling repos exist on disk."""
    if not related_repos:
        return ""
    return _RELATED_REPOS_TEMPLATE.render(repos=related_repos)


_MEMORY_PROTOCOL_TEMPLATE = Template(
    """\
=== Memory ===
ALWAYS VIEW YOUR MEMORY DIRECTORY FIRST, BEFORE DOING ANYTHING ELSE. Use \
the `view` command to see what's already known before you duplicate work \
discovering it again.

This memory tool (view/create/str_replace/insert/delete/rename) is durable, \
cross-run storage for this workspace's repos — NOT scratch space for this \
run. Anything you write here is still there the next time any agent runs \
against this workspace, potentially days or weeks from now and in a \
completely different container.

Use it for institutional knowledge that would otherwise be re-discovered \
the hard way every run: a Sentry error that turned out to be a known \
false positive and why, a correction a human made to a past review, a \
repo-specific gotcha (a flaky test, a non-obvious build step, a directory \
that looks relevant but isn't). Do NOT use it to track progress within \
this run, jot down scratch notes, or log what you're about to do — that's \
what your own reasoning and this run's output are for.

Keep entries small, specific, and dated where that matters — this is \
notes for your future self and other agents, not a diary.
=== End memory ==="""
)


def memory_protocol_block() -> str:
    """Build the block framing memory as durable, cross-run storage.

    A custom SDK tool gets none of the auto-injected framing Anthropic's
    built-in memory_20250818 tool gets for free, so it has to be spelled
    out here — deliberately different from that tool's default framing,
    which is a session-recovery/progress log for one task, not durable
    cross-run knowledge.
    """
    return _MEMORY_PROTOCOL_TEMPLATE.render()


_CONTINUITY_TEMPLATE = Template(
    """\
=== Continuity ===
The comments, notes, or discussion history in this prompt may already \
include turns from other jeanclode agents that worked on this same issue \
or PR/MR before you — a different workflow, a different run, possibly a \
different language or tone than this one. Read the full history in order \
before treating anything as open or undecided.

If a question was already asked and answered there, a decision was \
already made, or a finding was already raised, treat it as settled: don't \
re-ask it, re-decide it, or re-report it. Only revisit something if the \
record shows it genuinely changed since, or was never actually resolved.
=== End continuity ==="""
)


def continuity_protocol_block() -> str:
    """Build the block warning an agent that thread history it's about to
    read may contain prior turns from other jeanclode agents/workflows on
    the same issue or PR, so settled points there should stand rather than
    be re-derived — or re-asked, or re-posted — from scratch.
    """
    return _CONTINUITY_TEMPLATE.render()
