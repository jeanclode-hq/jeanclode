from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.activities.review import recover_failed_post
from src.activities.review.schemas import PRRef, UnpostedComment
from src.runtime.bus import EventBus
from src.runtime.context import RunContext

_STUB_BODY = """\
import json, os, sys
calls_path = os.environ["STUB_CALLS"]
with open(calls_path, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["api"] and "--input" in args:
    idx = args.index("--input")
    payload_path = args[idx + 1]
    if payload_path != "-":
        try:
            with open(payload_path) as fp:
                body = fp.read()
            with open(calls_path, "a") as f:
                f.write(json.dumps({"_payload": body}) + "\\n")
        except OSError:
            pass
sys.exit(0)
"""


@pytest.fixture
def stub_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("gh", "glab"):
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env python3\n" + _STUB_BODY)
        stub.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    calls.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STUB_CALLS", str(calls))
    return calls


def _read_calls(calls: Path) -> list[list[str] | dict]:
    return [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def test_recovers_all_unposted_regardless_of_error_type(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(platform="github", repo="o/r", pr="42")
    unposted = [
        UnpostedComment(path="x.py", line=1, body="finding A", error_type="out_of_diff"),
        UnpostedComment(path="y.py", line=2, body="finding B", error_type="rate_limited"),
        UnpostedComment(path="z.py", line=3, body="finding C", error_type="other"),
    ]
    result = recover_failed_post(unposted, pr, ctx=_ctx(tmp_path))
    assert result.recovered == 3
    assert result.skipped == 0


def test_recovery_body_lists_each_unposted_with_reason(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(platform="github", repo="o/r", pr="42")
    unposted = [
        UnpostedComment(path="x.py", line=1, body="finding A", error_type="out_of_diff"),
        UnpostedComment(path="y.py", line=2, body="finding B", error_type="rate_limited"),
    ]
    recover_failed_post(unposted, pr, ctx=_ctx(tmp_path))
    calls = [c for c in _read_calls(stub_env) if isinstance(c, list)]
    body_call = next(c for c in calls if c[:3] == ["pr", "comment", "42"])
    body = body_call[body_call.index("--body") + 1]
    assert "x.py:1" in body
    assert "y.py:2" in body
    assert "line outside the PR diff" in body
    assert "rate-limited" in body


def test_no_unposted_no_post(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(platform="github", repo="o/r", pr="42")
    result = recover_failed_post([], pr, ctx=_ctx(tmp_path))
    assert result.recovered == 0
    assert _read_calls(stub_env) == []


def test_gitlab_uses_top_level_note(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(
        platform="gitlab",
        repo="g/p",
        pr="9",
        pr_url="https://gitlab.example.com/g/p/-/merge_requests/9",
    )
    unposted = [
        UnpostedComment(path="x.py", line=1, body="b", error_type="out_of_diff"),
    ]
    result = recover_failed_post(unposted, pr, ctx=_ctx(tmp_path))
    assert result.recovered == 1
    assert "gitlab.example.com" in result.pr_url
    calls = [c for c in _read_calls(stub_env) if isinstance(c, list)]
    assert any(c[:3] == ["api", "--method", "POST"] for c in calls)
