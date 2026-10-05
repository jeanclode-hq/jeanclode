"""Upload the demo's screenshots or video and write them into the PR/MR description.

GitLab shows an image or plays a video uploaded to the project inline. GitHub
only plays video from its `user-attachments` storage, which an App
installation token can't upload to, so there the media (screenshots as they
are, a video as a GIF) is committed on its own, parentless, under a custom ref
(`refs/jeanclode/demo-<n>`): not a branch, not fetched by a default clone, and
the SHA-pinned embed renders for anyone who can read the repo. Nothing is
cleaned up afterwards.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import imageio_ffmpeg

from src.activities.decorator import activity
from src.activities.demo.block import render_demo_block, render_demo_link, with_demo_block
from src.activities.demo.schemas import DemoRound
from src.activities.git.pr import pr_id
from src.activities.git.schemas import PRRef
from src.runtime.context import RunContext

logger = logging.getLogger(__name__)

_GIF_FPS = 10
_GIF_WIDTH = 800
_UPLOAD_TIMEOUT_S = 120
_MAX_SCREENSHOTS = 4


def demo_media(demo: DemoRound) -> list[Path]:
    """The files the PR/MR gets: the kind the demo agent picked, else whichever it saved."""
    shots = [Path(s) for s in demo.screenshots if Path(s).is_file()][:_MAX_SCREENSHOTS]
    video = [Path(demo.video_path)] if demo.video_path and Path(demo.video_path).is_file() else []
    if demo.media == "screenshot":
        return shots or video
    return video or shots


def _repo_path(pr: PRRef) -> str:
    """``owner/repo`` (GitHub) or ``group/sub/project`` (GitLab) from the PR/MR URL."""
    path = urlparse(pr.url).path.strip("/")
    marker = "/pull/" if pr.platform == "github" else "/-/merge_requests/"
    return path.split(marker, 1)[0]


def _host(pr: PRRef) -> str:
    return urlparse(pr.url).netloc


def _run(cmd: list[str], *, ctx: RunContext, stdin: str | None = None) -> str:
    env = {**os.environ, **ctx.env}
    proc = subprocess.run(
        cmd, cwd=ctx.cwd, capture_output=True, text=True, input=stdin, env=env, check=False
    )
    if proc.returncode != 0:
        msg = f"{' '.join(cmd[:3])} failed (exit={proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def to_gif(video: Path, out: Path) -> Path:
    """Two-pass palette conversion: far smaller and cleaner than ffmpeg's default GIF.

    The static ffmpeg bundled with ``imageio-ffmpeg``: Debian's package pulls in
    ~400MB of GPU and LLVM libraries the image would carry for one conversion.
    """
    filters = (
        f"fps={_GIF_FPS},scale={_GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    )
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-filter_complex",
            filters,
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def _gh_api(path: str, payload: dict[str, Any], *, ctx: RunContext, method: str = "POST") -> dict:
    # The payload goes through a file: a base64 GIF is far past the per-argument limit.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
    try:
        out = _run(["gh", "api", "-X", method, path, "--input", fh.name], ctx=ctx)
    finally:
        Path(fh.name).unlink(missing_ok=True)
    return json.loads(out)


def publish_github_files(pr: PRRef, files: list[Path], *, ctx: RunContext) -> list[str]:
    """Commit ``files`` under ``refs/jeanclode/demo-<n>``; return their SHA-pinned embed URLs."""
    repo = _repo_path(pr)
    api = f"repos/{repo}/git"
    names = [
        f"demo-{i}{f.suffix}" if len(files) > 1 else f"demo{f.suffix}"
        for i, f in enumerate(files, 1)
    ]
    entries = []
    for name, file in zip(names, files, strict=True):
        blob = _gh_api(
            f"{api}/blobs",
            {"content": base64.b64encode(file.read_bytes()).decode(), "encoding": "base64"},
            ctx=ctx,
        )
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = _gh_api(f"{api}/trees", {"tree": entries}, ctx=ctx)
    commit = _gh_api(
        f"{api}/commits",
        {"message": f"jeanclode demo for #{pr_id(pr)}", "tree": tree["sha"], "parents": []},
        ctx=ctx,
    )
    ref = f"jeanclode/demo-{pr_id(pr)}"
    try:
        _gh_api(f"{api}/refs/{ref}", {"sha": commit["sha"], "force": True}, ctx=ctx, method="PATCH")
    except RuntimeError:
        _gh_api(f"{api}/refs", {"ref": f"refs/{ref}", "sha": commit["sha"]}, ctx=ctx)
    return [f"https://github.com/{repo}/blob/{commit['sha']}/{name}?raw=true" for name in names]


def upload_gitlab_file(pr: PRRef, file: Path, *, ctx: RunContext) -> str:
    """Upload through the project uploads API; returns the markdown GitLab renders inline.

    Plain HTTP rather than `glab api`, which can't send a multipart file. In a
    container the token is a placeholder the security proxy swaps for the real one.
    """
    project = quote(_repo_path(pr), safe="")
    token = ctx.env.get("GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN", "")
    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    with file.open("rb") as fh:
        resp = httpx.post(
            f"https://{_host(pr)}/api/v4/projects/{project}/uploads",
            headers={"PRIVATE-TOKEN": token},
            files={"file": (file.name, fh, content_type)},
            timeout=_UPLOAD_TIMEOUT_S,
        )
    resp.raise_for_status()
    return str(resp.json()["markdown"])


def _read_description(pr: PRRef, *, ctx: RunContext) -> str:
    if pr.platform == "github":
        return _run(
            ["gh", "pr", "view", pr_id(pr), "-R", _repo_path(pr), "--json", "body", "-q", ".body"],
            ctx=ctx,
        )
    raw = _run(["glab", "mr", "view", pr_id(pr), "-R", _repo_path(pr), "-F", "json"], ctx=ctx)
    return str(json.loads(raw).get("description") or "")


def _write_description(pr: PRRef, body: str, *, ctx: RunContext) -> None:
    if pr.platform == "github":
        _run(
            ["gh", "pr", "edit", pr_id(pr), "-R", _repo_path(pr), "--body-file", "-"],
            ctx=ctx,
            stdin=body,
        )
    else:
        _run(
            ["glab", "mr", "update", pr_id(pr), "-R", _repo_path(pr), "--description", body],
            ctx=ctx,
        )


def _glab_ctx(pr: PRRef, ctx: RunContext) -> RunContext:
    return ctx.model_copy(update={"env": {**ctx.env, "GITLAB_HOST": _host(pr)}})


@activity(name="Posting demo")
def publish_demo(pr: PRRef, demo: DemoRound, *, ctx: RunContext) -> str:
    """Upload ``demo``'s media and put it at the top of the PR/MR description.

    Returns the media markdown that went in. A re-recording replaces the block.
    """
    files = demo_media(demo)
    if not files:
        msg = "the demo saved no screenshot or video"
        raise RuntimeError(msg)
    if pr.platform == "github":
        files = [to_gif(f, f.with_suffix(".gif")) if f.suffix == ".webm" else f for f in files]
        media = "\n\n".join(f"![demo]({url})" for url in publish_github_files(pr, files, ctx=ctx))
    else:
        ctx = _glab_ctx(pr, ctx)
        media = "\n\n".join(upload_gitlab_file(pr, f, ctx=ctx) for f in files)
    block = render_demo_block(media)
    _write_description(pr, with_demo_block(_read_description(pr, ctx=ctx), block), ctx=ctx)
    return media


@activity(name="Linking demo")
def link_demo(pr: PRRef, app_pr: PRRef, *, ctx: RunContext) -> None:
    """Point ``pr``'s description at the demo posted on ``app_pr``."""
    if pr.platform == "gitlab":
        ctx = _glab_ctx(pr, ctx)
    block = render_demo_link(app_pr.url)
    _write_description(pr, with_demo_block(_read_description(pr, ctx=ctx), block), ctx=ctx)


@activity(name="Posting demo note")
def post_demo_note(pr: PRRef, text: str, *, ctx: RunContext) -> None:
    if pr.platform == "github":
        _run(
            ["gh", "pr", "comment", pr_id(pr), "-R", _repo_path(pr), "--body-file", "-"],
            ctx=ctx,
            stdin=text,
        )
    else:
        ctx = _glab_ctx(pr, ctx)
        _run(["glab", "mr", "note", pr_id(pr), "-R", _repo_path(pr), "--message", text], ctx=ctx)
