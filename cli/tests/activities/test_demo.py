"""Demo block rendering, the checkout helpers, and posting the recording."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from src.activities.demo import (
    BYPASS_FILE,
    DEMO_END,
    DEMO_START,
    HINTS_FILE,
    DemoRound,
    render_demo_block,
    render_demo_link,
    reset_demo_dir,
    split_demo_block,
    with_demo_block,
)
from src.activities.demo.publish import publish_demo, publish_github_gif, upload_gitlab_video
from src.activities.git.schemas import PRRef
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def test_split_demo_block_without_one() -> None:
    assert split_demo_block("just text") == ("", "just text")


def test_with_demo_block_replaces_the_previous_one_and_goes_on_top() -> None:
    old = f"Intro\n\n{DEMO_START}\nold\n{DEMO_END}\n\nMore"
    new_block = f"{DEMO_START}\nnew\n{DEMO_END}"
    out = with_demo_block(old, new_block)
    assert out.startswith(new_block)
    assert "old" not in out
    assert "Intro" in out and "More" in out


def test_with_demo_block_on_an_empty_description() -> None:
    block = f"{DEMO_START}\nx\n{DEMO_END}"
    assert with_demo_block("", block) == block + "\n"


def test_render_demo_block_fences_a_diff_containing_backticks() -> None:
    block = render_demo_block("![demo](u)", {"/a": "+```js\n+x\n+```"})
    assert block.startswith(DEMO_START) and block.endswith(DEMO_END)
    assert "````diff" in block
    assert "How this demo was set up" in block
    assert split_demo_block(f"{block}\n\nrest")[0] == block


def test_render_demo_block_without_setup_has_no_dropdown() -> None:
    assert "<details>" not in render_demo_block("![demo](u)", {"/a": ""})


def test_reset_demo_dir_keeps_hints_and_bypass(tmp_path: Path) -> None:
    for name in (HINTS_FILE, BYPASS_FILE, "demo.webm"):
        (tmp_path / name).write_text("x")
    (tmp_path / "shots").mkdir()
    reset_demo_dir(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([HINTS_FILE, BYPASS_FILE])


def _ok(cmd: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


def test_publish_github_gif_commits_under_a_custom_ref(tmp_path: Path) -> None:
    gif = tmp_path / "demo.gif"
    gif.write_bytes(b"GIF89a")
    pr = PRRef(url="https://github.com/o/r/pull/7", branch="fix/1", platform="github")
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(Path(cmd[cmd.index("--input") + 1]).read_text())
        seen.append((cmd, payload))
        if cmd[4].endswith("/refs/jeanclode/demo-7"):
            return subprocess.CompletedProcess(cmd, 1, "", "Reference does not exist")
        return _ok(cmd, json.dumps({"sha": f"sha{len(seen)}"}))

    with patch("src.activities.demo.publish.subprocess.run", side_effect=fake_run):
        url = publish_github_gif(pr, gif, ctx=_ctx(tmp_path))

    assert url == "https://github.com/o/r/blob/sha3/demo.gif?raw=true"
    paths = [cmd[4] for cmd, _ in seen]
    assert paths == [
        "repos/o/r/git/blobs",
        "repos/o/r/git/trees",
        "repos/o/r/git/commits",
        "repos/o/r/git/refs/jeanclode/demo-7",
        "repos/o/r/git/refs",
    ]
    assert seen[2][1]["parents"] == []
    assert seen[4][1] == {"ref": "refs/jeanclode/demo-7", "sha": "sha3"}


def test_upload_gitlab_video_returns_the_markdown(tmp_path: Path) -> None:
    video = tmp_path / "demo.webm"
    video.write_bytes(b"webm")
    pr = PRRef(
        url="https://gitlab.example.dev/g/sub/app/-/merge_requests/3",
        branch="fix/1",
        platform="gitlab",
    )
    with patch("src.activities.demo.publish.httpx.post") as post:
        post.return_value = httpx.Response(
            201,
            json={"markdown": "![demo](/uploads/abc/demo.webm)"},
            request=httpx.Request("POST", "https://x"),
        )
        md = upload_gitlab_video(pr, video, ctx=_ctx(tmp_path))

    assert md == "![demo](/uploads/abc/demo.webm)"
    assert (
        post.call_args.args[0] == "https://gitlab.example.dev/api/v4/projects/g%2Fsub%2Fapp/uploads"
    )


def test_publish_demo_writes_the_block_into_the_description(tmp_path: Path) -> None:
    video = tmp_path / "demo.webm"
    video.write_bytes(b"webm")
    pr = PRRef(url="https://gitlab.com/g/app/-/merge_requests/3", branch="fix/1", platform="gitlab")
    demo = DemoRound(verdict="ok", video_path=str(video), setup_diffs={"/a": "+mock"})
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["glab", "mr", "view"]:
            return _ok(cmd, json.dumps({"description": "Fixes the button"}))
        return _ok(cmd)

    with (
        patch("src.activities.demo.publish.subprocess.run", side_effect=fake_run),
        patch("src.activities.demo.publish.upload_gitlab_video", return_value="![demo](/u.webm)"),
    ):
        publish_demo(pr, demo, ctx=_ctx(tmp_path))

    update = next(c for c in calls if c[:3] == ["glab", "mr", "update"])
    body = update[update.index("--description") + 1]
    assert body.startswith(DEMO_START)
    assert "![demo](/u.webm)" in body and "+mock" in body
    assert body.rstrip().endswith("Fixes the button")


def test_render_demo_link_is_a_demo_block_pointing_at_the_app_pr() -> None:
    block = render_demo_link("https://gitlab.com/g/front/-/merge_requests/3")
    assert split_demo_block(f"Body\n\n{block}")[0] == block
    assert "https://gitlab.com/g/front/-/merge_requests/3" in block
