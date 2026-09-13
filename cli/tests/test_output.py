"""Tests for ProgressTracker output."""

from __future__ import annotations

import io
import re

from rich.console import Console

from src.agents.schemas import AgentUsage
from src.output import (
    ProgressTracker,
    _SpinnerWithTrail,
    emit_error,
    emit_rate_limit_error,
    extract_retry_after,
)

_ANSI_RE = re.compile(r"\x1b\[[^a-zA-Z]*[a-zA-Z]|\r")


def _clean(text: str) -> str:
    """Strip ANSI escapes and carriage returns for assertion-friendly output."""
    return _ANSI_RE.sub("", text)


def _make_tracker(
    issue_url: str = "https://myorg.sentry.io/issues/12345",
) -> tuple[ProgressTracker, io.StringIO]:
    """Create a tracker with a captured string buffer."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80, no_color=True, highlight=False)
    tracker = ProgressTracker(issue_url, version="0.1.0", model="sonnet", console=console)
    return tracker, buf


def test_start_prints_header() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.finish()
    output = _clean(buf.getvalue())
    assert "JeanClode" in output
    assert "v0.1.0" in output
    assert "https://myorg.sentry.io/issues/12345" in output
    assert "model:" in output
    assert "sonnet" in output


def test_step_and_done_via_finish() -> None:
    """done() stores detail, finish() prints the completed step."""
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Triaging issue...")
    tracker.done("null_reference, 3 files")
    tracker.finish()
    output = _clean(buf.getvalue())
    assert "✓" in output
    assert "Triaging issue..." in output
    assert "null_reference, 3 files" in output


def test_step_done_without_detail() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Working...")
    tracker.done()
    tracker.finish()
    output = _clean(buf.getvalue())
    assert "✓" in output
    assert "Working..." in output


def test_fail_prints_error() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Cloning repository...")
    tracker.fail("permission denied")
    output = _clean(buf.getvalue())
    assert "✗" in output or "Error" in output
    assert "permission denied" in output


def test_multiple_steps() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Step 1...")
    tracker.done("ok")
    tracker.step("Step 2...")
    tracker.done("ok")
    tracker.finish()
    output = _clean(buf.getvalue())
    assert output.count("✓") >= 2


def test_finish_without_active_step() -> None:
    tracker, _buf = _make_tracker()
    # Should not raise even without start()
    tracker.finish()


def test_step_replaces_previous_step() -> None:
    """Starting a new step prints the previous one as completed."""
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("First step...")
    tracker.done("done1")
    tracker.step("Second step...")
    tracker.done("done2")
    tracker.finish()
    output = _clean(buf.getvalue())
    assert "First step..." in output
    assert "Second step..." in output


def test_tool_trail() -> None:
    tracker, _buf = _make_tracker()
    tracker.start()
    tracker.step("Triaging...")
    tracker.on_tool_call("Read", "src/main.py")
    tracker.on_tool_call("Grep", "TODO in src/")
    # Trail should be updated
    assert len(tracker._tool_trail) == 2
    tracker.finish()


def test_tool_trail_filters_non_display_tools() -> None:
    tracker, _buf = _make_tracker()
    tracker.start()
    tracker.step("Working...")
    tracker.on_tool_call("Read", "file.py")
    tracker.on_tool_call("ToolSearch", "something")  # should be filtered
    assert len(tracker._tool_trail) == 1
    tracker.finish()


def test_make_tool_callback() -> None:
    tracker, _buf = _make_tracker()
    tracker.start()
    tracker.step("Working...")
    cb = tracker.make_tool_callback()
    cb("Bash", "git status", {})
    assert len(tracker._tool_trail) == 1
    tracker.finish()


def test_spinner_with_trail_renderable() -> None:
    display = _SpinnerWithTrail()
    display.trail = ["Read src/main.py", "Grep TODO"]
    assert len(display.trail) == 2


def test_done_with_usage() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Triaging...")
    tracker.done(usage="1,234 in · 567 out")
    tracker.finish()
    output = _clean(buf.getvalue())
    assert "1,234 in" in output


def test_summary_prints_total_usage() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Working...")
    tracker.done("ok")
    tracker.summary(AgentUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=50))
    output = _clean(buf.getvalue())
    assert "Done in" in output
    assert "1,000 in" in output
    assert "200 out" in output
    assert "50 cache read" in output


def test_fail_with_usage_prints_tokens() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Fixing...")
    tracker.fail("boom", AgentUsage(input_tokens=10, output_tokens=5))
    output = _clean(buf.getvalue())
    assert "boom" in output
    assert "10 in" in output


def test_fail_without_usage_omits_token_line() -> None:
    tracker, buf = _make_tracker()
    tracker.start()
    tracker.step("Fixing...")
    tracker.fail("boom")
    output = _clean(buf.getvalue())
    assert "boom" in output
    assert " in " not in output.split("boom")[-1].split("\n")[0]


def test_emit_error_includes_usage(capsys) -> None:
    emit_error(
        "unexpected",
        "boom",
        usage=AgentUsage(input_tokens=10, output_tokens=5),
    )
    err = capsys.readouterr().err
    assert '"input_tokens": 10' in err
    assert '"output_tokens": 5' in err


def test_emit_error_omits_usage_when_zero(capsys) -> None:
    emit_error("unexpected", "boom", usage=AgentUsage())
    err = capsys.readouterr().err
    assert "usage" not in err


def test_emit_rate_limit_error_carries_type_and_message(capsys) -> None:
    emit_rate_limit_error("rate limited")
    err = capsys.readouterr().err
    assert '"type": "rate_limit_error"' in err
    assert '"message": "rate limited"' in err


def test_emit_rate_limit_error_includes_credential_id_from_env(capsys, monkeypatch) -> None:
    monkeypatch.setenv("JEANCLODE_LLM_CREDENTIAL_ID", "cred-123")
    emit_rate_limit_error("rate limited")
    err = capsys.readouterr().err
    assert '"credential_id": "cred-123"' in err


def test_emit_rate_limit_error_omits_credential_id_when_env_unset(capsys, monkeypatch) -> None:
    monkeypatch.delenv("JEANCLODE_LLM_CREDENTIAL_ID", raising=False)
    emit_rate_limit_error("rate limited")
    err = capsys.readouterr().err
    assert "credential_id" not in err


def test_emit_rate_limit_error_includes_sentry_fix_branch_and_pr_url(capsys) -> None:
    emit_rate_limit_error(
        "rate limited",
        retry_after=30.0,
        branch="jeanclode/fix-123",
        pr_url="https://github.com/acme/app/pull/42",
    )
    err = capsys.readouterr().err
    assert '"retry_after": 30.0' in err
    assert '"branch": "jeanclode/fix-123"' in err
    assert '"pr_url": "https://github.com/acme/app/pull/42"' in err


def test_extract_retry_after_reads_attribute_when_present() -> None:
    class _Exc(Exception):
        retry_after = 45

    assert extract_retry_after(_Exc()) == 45.0


def test_extract_retry_after_parses_message_when_no_attribute() -> None:
    exc = Exception("rate_limit_error: please retry-after 12 seconds")
    assert extract_retry_after(exc) == 12.0


def test_extract_retry_after_none_when_nothing_found() -> None:
    assert extract_retry_after(Exception("some other failure")) is None
