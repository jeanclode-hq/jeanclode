"""Tests for the pr-summary workflow."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from src.activities.summary import (
    FILES_MARKER,
    FileLine,
    ParsedSummary,
    PRSnapshot,
    SummaryPayload,
    format_summary,
    strip_files_dropdown,
    update_pr_description,
)
from src.adaptors.diffn import DiffFile, prepare_diff
from src.agents.schemas import AgentResult
from src.agents.summary import (
    ParserAgent,
    ParserInput,
    SummarizerAgent,
    SummarizerInput,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import ActivityStart, AgentStart, Event, Panel
from src.workflows.pr_summary.runner import PRSummaryWorkflow
from src.workflows.pr_summary.utils import (
    build_file_lines,
    load_pr_snapshot,
    parse_description,
    render_file_list,
)

_REAL_DIFF = prepare_diff(
    "diff --git a/src/app.py b/src/app.py\n@@ -1,1 +1,2 @@\n-old\n+new\n+more\n"
    "diff --git a/uv.lock b/uv.lock\n@@ -1,1 +1,1 @@\n-pkg==1\n+pkg==2\n"
)

# ── helpers ──────────────────────────────────────────────────────────


def _write_pr_context(
    cwd: Path,
    *,
    platform: str = "github",
    repo: str = "o/r",
    pr: str = "1",
    pr_url: str = "https://github.com/o/r/pull/1",
    pr_description: str = "# Title\n\nFixes #5",
    diff: str = "diff --git a b\n+x\n",
) -> None:
    base = cwd / ".context"
    base.mkdir(parents=True, exist_ok=True)
    (base / "platform").write_text(platform + "\n")
    (base / "repo").write_text(repo + "\n")
    (base / "pr").write_text(pr + "\n")
    (base / "pr_url").write_text(pr_url + "\n")
    (base / "pr_description").write_text(pr_description)
    (base / "diff").write_text(diff)


def _ctx(tmp_path: Path, *, env: dict[str, str] | None = None) -> tuple[RunContext, list[Event]]:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    return (
        RunContext(cwd=tmp_path, env=env or {}, workspace=tmp_path, events=bus),
        received,
    )


def _result_message(text: str, structured: dict | None = None) -> ResultMessage:
    msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result=text,
    )
    if structured is not None:
        object.__setattr__(msg, "structured_output", structured)
    return msg


class _ScriptedQuery:
    """Returns scripted responses by matching prompt substrings."""

    def __init__(self) -> None:
        self.responses: list[tuple[str, str, dict | None]] = []

    def add(self, match: str, text: str, structured: dict | None = None) -> None:
        self.responses.append((match, text, structured))

    def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        for i, (match, text, structured) in enumerate(self.responses):
            if match in prompt:
                self.responses.pop(i)
                return self._gen(text, structured)
        return self._gen("{}", None)

    async def _gen(self, text: str, structured: dict | None) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        yield _result_message(text, structured)


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


# ── load_pr_snapshot / parse helpers ──────────────────────────────────


def test_load_pr_snapshot_reads_files(tmp_path: Path) -> None:
    _write_pr_context(tmp_path)
    snap = load_pr_snapshot(tmp_path)
    assert snap is not None
    assert snap.platform == "github"
    assert snap.repo == "o/r"
    assert snap.pr == "1"
    assert "Fixes #5" in snap.pr_description


def test_load_pr_snapshot_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_pr_snapshot(tmp_path) is None


def test_load_pr_snapshot_returns_none_when_required_fields_blank(tmp_path: Path) -> None:
    cache = tmp_path / ".context"
    cache.mkdir()
    (cache / "platform").write_text("github\n")
    (cache / "repo").write_text("\n")  # missing
    (cache / "pr").write_text("1\n")
    assert load_pr_snapshot(tmp_path) is None


def test_parse_description_prefers_structured() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text='{"description":"- raw"}', structured={"description": "- clean"})
    assert parse_description(r) == "- clean"


def test_parse_description_falls_back_to_text() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text="- bullet only", structured=None)
    assert parse_description(r) == "- bullet only"


def test_parse_description_unwraps_json_envelope_in_text() -> None:
    """Regression: when output_schema enforcement leaves ``structured``
    empty, ``text`` is the JSON envelope. We must unwrap it, otherwise
    the next agent gets ``{"description": "..."}`` as its draft and the
    final body double-wraps."""
    from src.agents.schemas import AgentResult

    r = AgentResult(text='{"description": "- Adds X\\n- Adds Y"}', structured=None)
    assert parse_description(r) == "- Adds X\n- Adds Y"


def test_parse_description_handles_fenced_json_in_text() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text='```json\n{"description":"- a"}\n```', structured=None)
    assert parse_description(r) == "- a"


def test_parse_description_unwraps_nested_envelope_in_structured() -> None:
    """Regression: model occasionally double-wraps when output_schema is
    on (``structured["description"]`` is itself a JSON-encoded
    envelope). Recursive unwrap should peel both layers."""
    from src.agents.schemas import AgentResult

    r = AgentResult(
        text="",
        structured={"description": '{"description": "- Adds X\\n- Adds Y"}'},
    )
    assert parse_description(r) == "- Adds X\n- Adds Y"


def test_parse_description_returns_plain_value_when_not_nested() -> None:
    from src.agents.schemas import AgentResult

    r = AgentResult(text="", structured={"description": "- ok"})
    assert parse_description(r) == "- ok"


def test_parse_description_collapses_double_escaped_newlines() -> None:
    """Regression: a model that over-escapes (``"\\\\n"`` instead of
    ``"\\n"`` inside the JSON payload) leaves a literal backslash-n in the
    decoded string. GitLab/GitHub then render both bullets as one line
    with ``\\n-`` printed as text (seen on PR #34's jeanclode:summary
    comment)."""
    from src.agents.schemas import AgentResult

    r = AgentResult(text="", structured={"description": "- Adds X\\n- Adds Y"})
    assert parse_description(r) == "- Adds X\n- Adds Y"


# ── format_summary ────────────────────────────────────────────────────


def test_format_summary_renders_the_description(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    payload = format_summary(ParsedSummary(description="- Adds X\n- Refactors Y"), ctx=ctx)
    assert payload.body.startswith("#### Description")
    assert "- Adds X" in payload.body
    assert payload.body.endswith("\n")


def test_format_summary_keeps_links_the_summary_wrote(tmp_path: Path) -> None:
    """Links live in the prose now — no separate section strips them out,
    and no section re-lists them either."""
    ctx, _ = _ctx(tmp_path)
    body = format_summary(
        ParsedSummary(
            description=(
                "- Guards the null ref, fixing "
                "https://sentry.example/organizations/acme/issues/1887793/\n"
                "- Depends on https://gitlab.example/o/r/-/merge_requests/12"
            )
        ),
        ctx=ctx,
    ).body
    assert "https://sentry.example/organizations/acme/issues/1887793/" in body
    assert "https://gitlab.example/o/r/-/merge_requests/12" in body
    assert "#### Links" not in body


def test_format_summary_renders_files_dropdown(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    body = format_summary(
        ParsedSummary(
            description="- Adds X",
            files=[
                FileLine(path="src/a.py", additions=3, deletions=1, summary="Adds\n  X  to a"),
                FileLine(path="b.py", old_path="old_b.py", summary=""),
            ],
        ),
        ctx=ctx,
    ).body
    assert body.startswith("#### Description\n\n- Adds X\n\n" + FILES_MARKER)
    assert "<details><summary>Changes per file (2)</summary>\n\n" in body
    assert "- `src/a.py` (+3 -1): Adds X to a\n" in body
    assert "- `old_b.py` → `b.py` (+0 -0)\n\n</details>" in body


def test_format_summary_caps_file_line_length(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    body = format_summary(
        ParsedSummary(description="- a", files=[FileLine(path="a.py", summary="word " * 100)]),
        ctx=ctx,
    ).body
    line = next(row for row in body.splitlines() if row.startswith("- `a.py`"))
    assert len(line) < 200
    assert line.endswith("…")


def test_format_summary_omits_dropdown_without_files(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    body = format_summary(ParsedSummary(description="- a"), ctx=ctx).body
    assert "<details>" not in body


def test_strip_files_dropdown_removes_only_the_marked_block(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    previous = format_summary(
        ParsedSummary(description="- Fixes https://x/1", files=[FileLine(path="a.py")]),
        ctx=ctx,
    ).body
    user_block = "<details><summary>Logs</summary>\n\nkeep me\n</details>\n"
    stripped = strip_files_dropdown(previous + user_block)
    assert "https://x/1" in stripped
    assert FILES_MARKER not in stripped
    assert "a.py" not in stripped
    assert "keep me" in stripped


def test_render_file_list_marks_noise_and_renames() -> None:
    rendered = render_file_list(
        [
            DiffFile(path="b.py", old_path="a.py", status="renamed"),
            DiffFile(path="uv.lock", additions=4, deletions=2, noise=True),
        ]
    )
    assert rendered == "- a.py -> b.py (renamed, +0 -0)\n- uv.lock (modified, +4 -2, noise)"


def test_build_file_lines_keeps_diff_order_and_drops_invented_paths() -> None:
    files = [DiffFile(path="a.py", additions=1), DiffFile(path="b.py")]
    result = AgentResult(
        text="",
        structured={
            "files": [
                {"path": "b.py", "summary": "Edits b"},
                {"path": "ghost.py", "summary": "Not in the diff"},
            ]
        },
    )
    lines = build_file_lines(files, result)
    assert [(line.path, line.summary) for line in lines] == [("a.py", ""), ("b.py", "Edits b")]


def test_build_file_lines_returns_nothing_when_no_summary_matches() -> None:
    files = [DiffFile(path="a.py")]
    assert build_file_lines(files, AgentResult(text="not json", structured=None)) == []
    junk = AgentResult(text="", structured={"files": [{"path": "ghost.py", "summary": "x"}]})
    assert build_file_lines(files, junk) == []


@pytest.mark.asyncio
async def test_workflow_posts_without_dropdown_when_file_summarizer_fails(tmp_path: Path) -> None:
    previous_dropdown = f"{FILES_MARKER}\n<details><summary>Changes per file (1)</summary>\n\n- `stale.py`\n\n</details>\n"
    _write_pr_context(
        tmp_path, pr_description="Does things\n\n" + previous_dropdown, diff=_REAL_DIFF
    )
    ctx, _ = _ctx(tmp_path)
    prompts: list[str] = []

    sq = _ScriptedQuery()
    sq.add("technical documentation assistant", "", {"description": "- a"})
    sq.add("expert editor", "", {"description": "- a"})

    def query_side_effect(*, prompt: str, options: Any) -> Any:
        prompts.append(prompt)
        if "one-line changelog entry" in prompt:
            raise RuntimeError("file summarizer down")
        return sq(prompt=prompt, options=options)

    with (
        patch("src.agents.base.query", side_effect=query_side_effect),
        patch("src.activities.summary.update_description.subprocess.run") as run_mock,
    ):
        run_mock.return_value = _completed(0)
        result = await PRSummaryWorkflow().run(ctx)

    assert result.status == "success"
    body = run_mock.call_args.args[0][-1]
    assert "- a" in body
    assert "<details>" not in body
    summarizer_prompt = next(p for p in prompts if "technical documentation assistant" in p)
    assert "stale.py" not in summarizer_prompt


def test_format_summary_emits_activity_event(tmp_path: Path) -> None:
    ctx, received = _ctx(tmp_path)
    format_summary(ParsedSummary(description="- a"), ctx=ctx)
    assert any(isinstance(e, ActivityStart) and e.name == "Formatting summary" for e in received)


# ── update_pr_description ───────────────────────────────────────────────


def test_update_pr_description_github(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    snap = PRSnapshot(
        platform="github", repo="o/r", pr="42", pr_url="https://github.com/o/r/pull/42"
    )
    with patch("src.activities.summary.update_description.subprocess.run") as run:
        run.return_value = _completed(0)
        result = update_pr_description(SummaryPayload(body="hello"), snap, ctx=ctx)
    assert result.posted is True
    assert result.pr_url == "https://github.com/o/r/pull/42"
    cmd = run.call_args.args[0]
    assert cmd == ["gh", "pr", "edit", "42", "-R", "o/r", "--body", "hello"]


def test_update_pr_description_gitlab_self_hosted_uses_url_host(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    snap = PRSnapshot(
        platform="gitlab",
        repo="g/p",
        pr="7",
        pr_url="https://gitlab.example.com/g/p/-/merge_requests/7",
    )
    with patch("src.activities.summary.update_description.subprocess.run") as run:
        run.return_value = _completed(0)
        result = update_pr_description(SummaryPayload(body="b"), snap, ctx=ctx)
    assert result.posted is True
    assert result.pr_url.startswith("https://gitlab.example.com/")
    cmd = run.call_args.args[0]
    assert cmd == ["glab", "mr", "update", "7", "-R", "g/p", "--description", "b"]


def test_update_pr_description_failure_records_stderr(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    snap = PRSnapshot(platform="github", repo="o/r", pr="1", pr_url="https://github.com/o/r/pull/1")
    with patch("src.activities.summary.update_description.subprocess.run") as run:
        run.return_value = _completed(1, stderr="boom")
        result = update_pr_description(SummaryPayload(body="b"), snap, ctx=ctx)
    assert result.posted is False
    assert result.error == "boom"


# ── agents (mock SDK) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_agent_renders_inputs(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt

        async def gen() -> AsyncIterator[Any]:
            yield _result_message('{"description":"- Adds X"}', {"description": "- Adds X"})

        return gen()

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await SummarizerAgent().invoke(
            SummarizerInput(pr_description="desc here", diff="diff here"),
            ctx,
        )

    assert "desc here" in captured["prompt"]
    assert "diff here" in captured["prompt"]
    assert result.structured == {"description": "- Adds X"}


@pytest.mark.asyncio
async def test_parser_agent_renders_draft(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> Any:
        captured["prompt"] = prompt

        async def gen() -> AsyncIterator[Any]:
            yield _result_message('{"description":"- refined"}', {"description": "- refined"})

        return gen()

    with patch("src.agents.base.query", side_effect=fake_query):
        result = await ParserAgent().invoke(ParserInput(draft="- a\n- b"), ctx)

    assert "- a" in captured["prompt"]
    assert result.structured == {"description": "- refined"}


# ── workflow integration ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_runs_full_pipeline_and_posts(tmp_path: Path) -> None:
    _write_pr_context(tmp_path, pr_description="Fixes #5\n\nDoes things", diff=_REAL_DIFF)
    ctx, received = _ctx(tmp_path)

    sq = _ScriptedQuery()
    files = {
        "files": [
            {"path": "src/app.py", "summary": "Replaces old with new"},
            {"path": "uv.lock", "summary": "Updates lockfile"},
        ]
    }
    sq.add("one-line changelog entry", "", files)
    sq.add(
        "technical documentation assistant",
        '{"description":"- Adds A, fixing https://github.com/o/r/issues/5\\n- Adds B"}',
        {"description": "- Adds A, fixing https://github.com/o/r/issues/5\n- Adds B"},
    )
    sq.add(
        "expert editor",
        '{"description":"- Adds A and B, fixing https://github.com/o/r/issues/5"}',
        {"description": "- Adds A and B, fixing https://github.com/o/r/issues/5"},
    )

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.summary.update_description.subprocess.run") as run_mock,
    ):
        run_mock.return_value = _completed(0)
        result = await PRSummaryWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["posted"] is True
    assert result.data["pr_url"] == "https://github.com/o/r/pull/1"

    # the refined description is posted, links included — the summarizer
    # carries them through its prose, nothing re-attaches them afterwards
    body = run_mock.call_args.args[0][-1]
    assert "https://github.com/o/r/issues/5" in body
    assert "- Adds A and B" in body

    assert "- `src/app.py` (+2 -1): Replaces old with new" in body
    assert "- `uv.lock` (+1 -1): Updates lockfile" in body

    agent_names = [e.name for e in received if isinstance(e, AgentStart)]
    assert sorted(agent_names) == ["File Summarizer", "Parser", "Summarizer"]
    activity_names = [e.name for e in received if isinstance(e, ActivityStart)]
    assert "Formatting summary" in activity_names
    assert "Updating PR description" in activity_names

    # final summary panel emitted
    panels = [e for e in received if isinstance(e, Panel)]
    assert panels and panels[-1].title == "Description Updated"
    assert "https://github.com/o/r/pull/1" in panels[-1].content


@pytest.mark.asyncio
async def test_workflow_dry_run_skips_post(tmp_path: Path) -> None:
    _write_pr_context(tmp_path)
    ctx, received = _ctx(tmp_path)
    object.__setattr__(ctx, "dry_run", True)

    sq = _ScriptedQuery()
    sq.add(
        "Issue Explorer",
        '{"context":"none","issue_refs":[]}',
        {"context": "No linked issues found.", "issue_refs": []},
    )
    sq.add("technical documentation assistant", '{"description":"- a"}', {"description": "- a"})
    sq.add("expert editor", '{"description":"- a"}', {"description": "- a"})

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.summary.update_description.subprocess.run") as run_mock,
    ):
        result = await PRSummaryWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["posted"] is False
    run_mock.assert_not_called()

    panels = [e for e in received if isinstance(e, Panel)]
    assert panels and panels[-1].title == "Summary (dry-run)"
    assert panels[-1].style == "yellow"


@pytest.mark.asyncio
async def test_workflow_post_failure_returns_error_status(tmp_path: Path) -> None:
    _write_pr_context(tmp_path)
    ctx, received = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add(
        "Issue Explorer",
        '{"context":"none","issue_refs":[]}',
        {"context": "No linked issues found.", "issue_refs": []},
    )
    sq.add("technical documentation assistant", '{"description":"- a"}', {"description": "- a"})
    sq.add("expert editor", '{"description":"- a"}', {"description": "- a"})

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.summary.update_description.subprocess.run") as run_mock,
    ):
        run_mock.return_value = _completed(1, stderr="auth failed")
        result = await PRSummaryWorkflow().run(ctx)

    assert result.status == "error"
    assert "auth failed" in result.summary
    panels = [e for e in received if isinstance(e, Panel)]
    assert panels and panels[-1].title == "Description Update Failed"
    assert panels[-1].style == "red"


@pytest.mark.asyncio
async def test_workflow_returns_error_when_pr_context_missing(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    result = await PRSummaryWorkflow().run(ctx)
    assert result.status == "error"
    assert ".context" in result.summary


@pytest.mark.asyncio
async def test_workflow_recovers_when_issue_explorer_fails(tmp_path: Path) -> None:
    """A failing explorer should not abort the whole pipeline — fall back
    to ``No linked issues found.`` and continue."""
    _write_pr_context(tmp_path)
    ctx, _ = _ctx(tmp_path)

    sq = _ScriptedQuery()
    # No "Issue Explorer" entry — falls through to the default response.
    # Make the explorer fail explicitly by raising in its branch.

    def query_side_effect(*, prompt: str, options: Any) -> Any:
        if "Issue Explorer" in prompt:
            raise RuntimeError("explorer down")
        return sq(prompt=prompt, options=options)

    sq.add("technical documentation assistant", '{"description":"- a"}', {"description": "- a"})
    sq.add("expert editor", '{"description":"- a"}', {"description": "- a"})

    with (
        patch("src.agents.base.query", side_effect=query_side_effect),
        patch("src.activities.summary.update_description.subprocess.run") as run_mock,
    ):
        run_mock.return_value = _completed(0)
        result = await PRSummaryWorkflow().run(ctx)

    assert result.status == "success"
    body = run_mock.call_args.args[0][-1]
    assert "#### Linked issues" not in body  # no refs → section omitted
