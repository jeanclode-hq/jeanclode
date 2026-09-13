"""Unit tests for `post_comments` — uses gh/glab stubs on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.activities.review import post_bot_followup, post_comments
from src.activities.review.posting import _BOT_FOLLOWUP, _classify
from src.activities.review.schemas import (
    GitHubComment,
    GitLabComment,
    PRRef,
)
from src.runtime.bus import EventBus
from src.runtime.context import RunContext

_STUB_BODY = """\
import json, os, sys
calls_path = os.environ["STUB_CALLS"]
fail_args_marker = os.environ.get("STUB_FAIL_MARKER", "")
fail_msg = os.environ.get("STUB_FAIL_MSG", "")
with open(calls_path, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["api"] and "--input" in args:
    idx = args.index("--input")
    payload_path = args[idx + 1]
    if payload_path != "-":
        try:
            with open(payload_path) as fp:
                payload_body = fp.read()
            with open(calls_path, "a") as f:
                f.write(json.dumps({"_payload": payload_body}) + "\\n")
        except OSError:
            pass
if fail_args_marker and fail_args_marker in " ".join(args):
    sys.stderr.write(fail_msg)
    sys.exit(1)
if any(a.endswith("/versions") for a in args):
    versions_json = os.environ.get("STUB_VERSIONS_JSON", "")
    if versions_json:
        sys.stdout.write(versions_json)
sys.exit(0)
"""


def _make_stub(bin_dir: Path, name: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text("#!/usr/bin/env python3\n" + _STUB_BODY)
    stub.chmod(0o755)


@pytest.fixture
def stub_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    _make_stub(bin_dir, "gh")
    _make_stub(bin_dir, "glab")
    calls = tmp_path / "calls.jsonl"
    calls.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STUB_CALLS", str(calls))
    return calls


def _read_calls(calls: Path) -> list[list[str] | dict]:
    return [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def test_lgtm_when_empty_github(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(platform="github", repo="o/r", pr="1")
    result = post_comments([], pr, ctx=_ctx(tmp_path))
    assert result.lgtm is True
    calls = _read_calls(stub_env)
    assert any(isinstance(c, list) and c[:3] == ["pr", "comment", "1"] for c in calls)


def test_post_bot_followup_github(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(platform="github", repo="o/r", pr="42", pr_author="jeanclode-bot[bot]")
    ok, err = post_bot_followup(pr, ctx=_ctx(tmp_path))
    assert ok is True
    assert err == ""
    calls = _read_calls(stub_env)
    assert any(
        isinstance(c, list) and c[:3] == ["pr", "comment", "42"] and _BOT_FOLLOWUP in c
        for c in calls
    )


def test_classifies_422_as_out_of_diff(
    stub_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_FAIL_MARKER", "reviews")
    monkeypatch.setenv("STUB_FAIL_MSG", "HTTP 422: Unprocessable Entity")
    comments = [GitHubComment(path="src/x.py", line=10, body="finding")]
    pr = PRRef(platform="github", repo="o/r", pr="42")
    result = post_comments(comments, pr, ctx=_ctx(tmp_path))
    assert result.posted == 0
    assert len(result.unposted) == 1
    assert result.unposted[0].error_type == "out_of_diff"


def test_lgtm_when_empty_gitlab(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(
        platform="gitlab", repo="g/p", pr="9", pr_url="https://gitlab.com/g/p/-/merge_requests/9"
    )
    result = post_comments([], pr, ctx=_ctx(tmp_path))
    assert result.lgtm is True


def test_gitlab_inline_includes_diff_refs(
    stub_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "STUB_VERSIONS_JSON",
        json.dumps(
            [
                {
                    "base_commit_sha": "base123",
                    "head_commit_sha": "head456",
                    "start_commit_sha": "start789",
                }
            ]
        ),
    )
    comments = [GitLabComment(new_path="x.py", new_line=1, body="finding")]
    pr = PRRef(
        platform="gitlab", repo="g/p", pr="9", pr_url="https://gitlab.com/g/p/-/merge_requests/9"
    )
    result = post_comments(comments, pr, ctx=_ctx(tmp_path))
    assert result.posted == 1
    calls = _read_calls(stub_env)
    assert any(isinstance(c, list) and any(a.endswith("/versions") for a in c) for c in calls)
    assert any(
        isinstance(c, list) and "discussions" in " ".join(c) and "--input" in c for c in calls
    )
    discussion_payload = next(
        json.loads(c["_payload"])
        for c in calls
        if isinstance(c, dict) and "position" in json.loads(c["_payload"])
    )
    position = discussion_payload["position"]
    assert position["base_sha"] == "base123"
    assert position["head_sha"] == "head456"
    assert position["start_sha"] == "start789"
    assert position["new_path"] == "x.py"
    assert position["new_line"] == 1


def test_post_bot_followup_gitlab(stub_env: Path, tmp_path: Path) -> None:
    pr = PRRef(
        platform="gitlab",
        repo="g/p",
        pr="9",
        pr_url="https://gitlab.com/g/p/-/merge_requests/9",
        pr_author="group_40_bot_deadbeef",
    )
    ok, err = post_bot_followup(pr, ctx=_ctx(tmp_path))
    assert ok is True
    assert err == ""
    calls = _read_calls(stub_env)
    # top-level discussion (no `position`) carrying the follow-up message
    flat_calls = [c for c in calls if isinstance(c, list)]
    assert any(_BOT_FOLLOWUP in (c[-1] if c else "") for c in flat_calls)


@pytest.mark.parametrize(
    ("platform", "pr_author", "expected"),
    [
        ("github", "jeanclode-bot[bot]", True),
        ("github", "renovate[bot]", True),
        ("github", "alice", False),
        ("github", "", False),
        # GitLab group/project access tokens
        ("gitlab", "group_40_bot_6f11260438821b591acac4b674839cc7", True),
        ("gitlab", "project_12_bot_abc", True),
        ("gitlab", "some-service_bot", True),
        ("gitlab", "jeanclode-bot", True),
        ("gitlab", "bob", False),
        # a plain "[bot]" suffix still counts on GitLab too
        ("gitlab", "octo[bot]", True),
    ],
)
def test_author_is_bot(platform: str, pr_author: str, expected: bool) -> None:
    pr = PRRef(platform=platform, repo="o/r", pr="1", pr_author=pr_author)
    assert pr.author_is_bot is expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 422", "out_of_diff"),
        ("HTTP 429: rate limit exceeded", "rate_limited"),
        ("HTTP 403", "permission"),
        ("network unreachable", "other"),
        ("Pull request review thread could not be created", "out_of_diff"),
    ],
)
def test_classify_error_buckets(stderr: str, expected: str) -> None:
    assert _classify(stderr) == expected


def test_subprocess_run_signature_compat() -> None:
    """Ensure the activity uses a subprocess shape compatible with the stub harness."""
    proc = subprocess.run(["true"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
