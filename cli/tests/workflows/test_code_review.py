from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from src.activities.review.schemas import PostResult, RecoverResult, UnpostedComment
from src.runtime.bus import EventBus
from src.runtime.context import RunContext
from src.runtime.events import Panel
from src.workflows.code_review.runner import CodeReviewWorkflow


def _setup_pr_context(repo_dir: Path, *, with_diff: bool = True, pr_author: str = "") -> None:
    cache = repo_dir / ".context"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "platform").write_text("github\n")
    (cache / "repo").write_text("o/r\n")
    (cache / "pr").write_text("42\n")
    (cache / "pr_url").write_text("https://github.com/o/r/pull/42\n")
    (cache / "pr_description").write_text("# title\n\nfix bug")
    diff = "diff --git a/x.py b/x.py\n+def f():\n+    pass\n" if with_diff else ""
    (cache / "diff").write_text(diff)
    (cache / "discussions").write_text(
        "## Review threads\n_None._\n\n## Review submissions\n_None._\n\n## Top-level comments\n_None._\n"
    )
    (cache / "pr_author").write_text(f"{pr_author}\n")


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


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
    """Returns scripted responses by matching prompt substrings.

    Each invocation pops from `responses` based on a substring match. If
    `parallel_capture` is set, captures the entry/exit times of each
    matching call so the test can assert true parallelism.
    """

    def __init__(self) -> None:
        self.responses: list[tuple[str, str, dict | None]] = []
        self.parallel_starts: list[tuple[str, float]] = []
        self.parallel_ends: list[tuple[str, float]] = []

    def add(self, match: str, text: str, structured: dict | None = None) -> None:
        self.responses.append((match, text, structured))

    def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        for i, (match, text, structured) in enumerate(self.responses):
            if match in prompt:
                self.responses.pop(i)
                tag = match
                return self._gen(text, structured, tag)
        return self._gen("{}", None, "fallback")

    async def _gen(self, text: str, structured: dict | None, tag: str) -> AsyncIterator[Any]:
        loop = asyncio.get_event_loop()
        self.parallel_starts.append((tag, loop.time()))
        # Yield to the event loop so concurrent gather actually overlaps.
        await asyncio.sleep(0.05)
        self.parallel_ends.append((tag, loop.time()))
        yield _result_message(text, structured)


def _captured_panels(ctx: RunContext) -> list[Panel]:
    received: list[Panel] = []
    ctx.events.subscribe(lambda e: received.append(e) if isinstance(e, Panel) else None)
    return received


@pytest.mark.asyncio
async def test_runs_full_pipeline_and_posts(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)
    panels = _captured_panels(ctx)

    sq = _ScriptedQuery()
    # Issue explorer
    sq.add(
        "linked issues",
        '{"context":"none","issue_refs":[]}',
        {"context": "No linked issues found.", "issue_refs": []},
    )
    # Two analyzers — distinguish by a unique marker only the analyzer prompt has.
    sq.add(
        "Analyzer[0]" if False else "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug A","side":"RIGHT"}]}',
    )
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug B","side":"RIGHT"}]}',
    )
    # Synthesizer (also has <diff> in prompt; keep it after analyzers consume).
    # Above two analyzers will run first because they run before synth in the
    # workflow; the next "<diff>" match is the synthesizer.
    sq.add(
        "<findings>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}',
    )
    # Fact-checker — has <comments> + <linked_issues>, distinguish by content
    sq.add(
        "skeptical investigator",
        '{"keep_indices":[0]}',
        {"keep_indices": [0]},
    )
    # Styler
    sq.add(
        "Code Review Formatter",
        '{"bodies":["styled body"]}',
        {"bodies": ["styled body"]},
    )

    posted = PostResult(posted=1, pr_url="https://github.com/o/r/pull/42")

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted) as post_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["comments_posted"] == 1
    # Verify the styled body actually flowed through to the post call.
    posted_comments = post_mock.call_args.args[0]
    assert posted_comments[0].body == "styled body"

    titles = [p.title for p in panels]
    assert "x.py:1" in titles
    panel = next(p for p in panels if p.title == "x.py:1")
    assert panel.content == "styled body"


@pytest.mark.asyncio
async def test_analyzers_run_in_parallel(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[]}')  # Analyzer[0]
    sq.add("<diff>", '{"comments":[]}')  # Analyzer[1]

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
    ):
        await CodeReviewWorkflow().run(ctx)

    # Two analyzer invocations should overlap — first start happens before second end.
    diff_starts = [t for tag, t in sq.parallel_starts if tag == "<diff>"]
    diff_ends = [t for tag, t in sq.parallel_ends if tag == "<diff>"]
    assert len(diff_starts) >= 2
    assert len(diff_ends) >= 2
    # second analyzer started before first ended → parallel
    assert diff_starts[1] < diff_ends[0]


@pytest.mark.asyncio
async def test_recovers_unposted_findings(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}',
    )
    sq.add("<diff>", '{"comments":[]}')
    sq.add("<findings>", '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}')
    sq.add("skeptical investigator", '{"keep_indices":[0]}', {"keep_indices": [0]})
    sq.add("Code Review Formatter", '{"bodies":["b"]}', {"bodies": ["b"]})

    post = PostResult(
        posted=0,
        pr_url="https://github.com/o/r/pull/42",
        unposted=[UnpostedComment(path="x.py", line=1, body="b", error_type="out_of_diff")],
    )
    recover = RecoverResult(recovered=1, pr_url="https://github.com/o/r/pull/42")

    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=post),
        patch(
            "src.workflows.code_review.runner.recover_failed_post", return_value=recover
        ) as recover_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert recover_mock.called
    assert "recover" in result.data


@pytest.mark.asyncio
async def test_stops_when_filter_says_stop(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path, with_diff=False)
    ctx = _ctx(tmp_path)
    panels = _captured_panels(ctx)

    with patch("src.agents.base.query") as query_mock:
        result = await CodeReviewWorkflow().run(ctx)

    assert result.status == "success"
    assert result.data["action"] == "stop"
    assert query_mock.call_count == 0
    assert any(p.title == "Skipped" for p in panels)


@pytest.mark.asyncio
async def test_survives_one_analyzer_failure(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)

    call_count = {"n": 0}

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            if "<diff>" in prompt:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("analyzer crashed")
                yield _result_message(
                    '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}'
                )
                return
            if "linked issues" in prompt:
                yield _result_message(
                    "{}", {"context": "No linked issues found.", "issue_refs": []}
                )
                return
            if "<findings>" in prompt:
                yield _result_message(
                    '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}'
                )
                return
            if "skeptical investigator" in prompt:
                yield _result_message('{"keep_indices":[0]}', {"keep_indices": [0]})
                return
            if "Code Review Formatter" in prompt:
                yield _result_message('{"bodies":["b"]}', {"bodies": ["b"]})
                return
            yield _result_message("{}")

        return gen()

    posted = PostResult(posted=1, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=fake_query),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert result.status == "success"


@pytest.mark.asyncio
async def test_errors_without_posting_when_every_analyzer_fails(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            if "<diff>" in prompt:
                raise RuntimeError("analyzer crashed")
            yield _result_message("{}", {"context": "No linked issues found.", "issue_refs": []})

        return gen()

    with (
        patch("src.agents.base.query", side_effect=fake_query),
        patch("src.workflows.code_review.runner.post_comments") as post_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert result.status == "error"
    post_mock.assert_not_called()


@pytest.mark.asyncio
async def test_returns_lgtm_when_no_findings(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)
    panels = _captured_panels(ctx)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[]}')
    sq.add("<diff>", '{"comments":[]}')

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted) as post_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert any(p.title == "LGTM" for p in panels)

    assert result.status == "success"
    assert result.data["lgtm"] is True
    args, _ = post_mock.call_args
    assert args[0] == []  # empty comments → LGTM path


@pytest.mark.asyncio
async def test_posts_bot_followup_on_bot_authored_pr_with_findings(tmp_path: Path) -> None:
    """Posts the top-level follow-up when the bot's own PR actually has findings to handle."""
    _setup_pr_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug A","side":"RIGHT"}]}',
    )
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug B","side":"RIGHT"}]}',
    )
    sq.add("<findings>", '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}')
    sq.add("skeptical investigator", '{"keep_indices":[0]}', {"keep_indices": [0]})
    sq.add("Code Review Formatter", '{"bodies":["b"]}', {"bodies": ["b"]})

    posted = PostResult(posted=1, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch(
            "src.workflows.code_review.runner.post_bot_followup", return_value=(True, "")
        ) as followup_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert followup_mock.called
    assert result.data["post"]["loop_triggered"] is True


@pytest.mark.asyncio
async def test_skips_bot_followup_on_lgtm(tmp_path: Path) -> None:
    """LGTM is a terminating status — no point pinging the bot when there's nothing to handle."""
    _setup_pr_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[]}')
    sq.add("<diff>", '{"comments":[]}')

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch("src.workflows.code_review.runner.post_bot_followup") as followup_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert not followup_mock.called
    assert result.data["post"]["loop_triggered"] is False


@pytest.mark.asyncio
async def test_skips_bot_followup_on_human_authored_pr(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path, pr_author="alice")
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug A","side":"RIGHT"}]}',
    )
    sq.add(
        "<diff>",
        '{"comments":[{"path":"x.py","line":1,"body":"bug B","side":"RIGHT"}]}',
    )
    sq.add("<findings>", '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}')
    sq.add("skeptical investigator", '{"keep_indices":[0]}', {"keep_indices": [0]})
    sq.add("Code Review Formatter", '{"bodies":["b"]}', {"bodies": ["b"]})

    posted = PostResult(posted=1, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch("src.workflows.code_review.runner.post_bot_followup") as followup_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert not followup_mock.called
    assert result.data["post"]["loop_triggered"] is False


@pytest.mark.asyncio
async def test_lgtm_post_failure_returns_error(tmp_path: Path) -> None:
    _setup_pr_context(tmp_path)
    ctx = _ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[]}')
    sq.add("<diff>", '{"comments":[]}')

    failed_lgtm = PostResult(
        lgtm=False,
        failed=1,
        pr_url="https://github.com/o/r/pull/42",
        errors=["gh failed"],
    )
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.workflows.code_review.runner.post_comments", return_value=failed_lgtm),
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert result.status == "error"


# ── ready notice on LGTM ──────────────────────────────────────────────


def _notify_ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        cwd=tmp_path,
        workspace=tmp_path,
        events=EventBus(),
        notify_users=["alice", "bob"],
    )


def _clean_review_query() -> _ScriptedQuery:
    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[]}')
    sq.add("<diff>", '{"comments":[]}')
    return sq


@pytest.mark.asyncio
async def test_notifies_on_lgtm_for_a_bot_authored_pr(tmp_path: Path) -> None:
    """LGTM on a bot-opened PR is the loop's exit — first moment the MR is
    in final shape and worth a human's attention."""
    _setup_pr_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx = _notify_ctx(tmp_path)

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=_clean_review_query()),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch(
            "src.workflows.code_review.runner.post_ready_notice", return_value=True
        ) as notice_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert notice_mock.called
    assert notice_mock.call_args.args[3] == ["alice", "bob"]
    assert result.data["notified"] is True


@pytest.mark.asyncio
async def test_no_notice_when_findings_remain(tmp_path: Path) -> None:
    """Findings mean the bot follow-up sweep runs instead — the loop is
    still going, so summoning humans now would be premature."""
    _setup_pr_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx = _notify_ctx(tmp_path)

    sq = _ScriptedQuery()
    sq.add("linked issues", "{}", {"context": "No linked issues found.", "issue_refs": []})
    sq.add("<diff>", '{"comments":[{"path":"x.py","line":1,"body":"bug A","side":"RIGHT"}]}')
    sq.add("<diff>", '{"comments":[{"path":"x.py","line":1,"body":"bug B","side":"RIGHT"}]}')
    sq.add("<findings>", '{"comments":[{"path":"x.py","line":1,"body":"bug","side":"RIGHT"}]}')
    sq.add("skeptical investigator", '{"keep_indices":[0]}', {"keep_indices": [0]})
    sq.add("Code Review Formatter", '{"bodies":["b"]}', {"bodies": ["b"]})

    posted = PostResult(posted=1, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=sq),
        patch("src.activities.review.guardrail._scan", return_value=False),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch("src.workflows.code_review.runner.post_bot_followup", return_value=(True, "")),
        patch("src.workflows.code_review.runner.post_ready_notice") as notice_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert not notice_mock.called
    assert result.data["notified"] is False


@pytest.mark.asyncio
async def test_no_notice_on_a_human_authored_pr(tmp_path: Path) -> None:
    """A clean review on someone's own PR is not Jeanclode reporting work
    done — the author already knows they opened it."""
    _setup_pr_context(tmp_path, pr_author="alice")
    ctx = _notify_ctx(tmp_path)

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=_clean_review_query()),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
        patch("src.workflows.code_review.runner.post_ready_notice") as notice_mock,
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert not notice_mock.called
    assert result.data["notified"] is False


@pytest.mark.asyncio
async def test_notice_is_attempted_but_reports_false_when_nobody_configured(
    tmp_path: Path,
) -> None:
    """The activity itself no-ops on an empty list, so the workflow doesn't
    need its own guard — but it must report honestly that nobody was told."""
    _setup_pr_context(tmp_path, pr_author="jeanclode-bot[bot]")
    ctx = _ctx(tmp_path)  # notify_users empty

    posted = PostResult(lgtm=True, pr_url="https://github.com/o/r/pull/42")
    with (
        patch("src.agents.base.query", side_effect=_clean_review_query()),
        patch("src.workflows.code_review.runner.post_comments", return_value=posted),
    ):
        result = await CodeReviewWorkflow().run(ctx)

    assert result.data["notified"] is False
