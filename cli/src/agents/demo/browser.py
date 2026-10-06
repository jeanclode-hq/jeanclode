"""Drive headless Chromium for a demo. The demo agent's scripts import this:

    from src.agents.demo.browser import ready, recording

    with recording() as page:
        page.goto("http://localhost:5173/orders")
        page.get_by_text("Order #1042").wait_for()
        ready(page)  # the video starts here
        page.screenshot(path="/tmp/jeanclode-demo/orders.png")

The video lands at ``<out_dir>/demo.webm`` once the block exits, cut to start
at ``ready(page)``, or at the first page load when the script never calls it.
Every page gets the overlay layer in ``layer.js`` (``window.__demo.root``) for
annotations, with a cursor in it when recording video. ``recording(video=False)``
is the same browser for screenshots only.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Page, ViewportSize, sync_playwright

from src.activities.demo.checkout import DEMO_DIR

logger = logging.getLogger(__name__)

VIEWPORT: ViewportSize = {"width": 1280, "height": 720}

# Only localhost resolves, and nothing goes through the egress proxy: whatever
# the app calls outside it has to be mocked (`page.route`) or it fails visibly.
_CHROMIUM_ARGS = [
    "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
]

_LAYER_JS = (Path(__file__).parent / "layer.js").read_text().strip().rstrip(";")

# Under this, a cut isn't worth re-encoding the video.
_MIN_TRIM_S = 0.3

# Per page: when its recording began, and when it was ready to show.
_started: dict[int, float] = {}
_ready: dict[int, float] = {}


def ready(page: Page) -> None:
    """Mark the moment the page shows what the demo is about: everything before is cut."""
    _ready[id(page)] = time.monotonic()


def trim_start(video: Path, seconds: float) -> None:
    """Cut the first ``seconds`` of ``video`` in place; leaves it as is if ffmpeg fails."""
    if seconds < _MIN_TRIM_S:
        return
    out = video.with_name(f"{video.stem}.trimmed{video.suffix}")
    proc = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds:.2f}",
            "-i",
            str(video),
            "-an",
            "-c:v",
            "libvpx",
            "-crf",
            "8",
            "-b:v",
            "2M",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not out.is_file():
        logger.warning("could not trim %s: %s", video, proc.stderr.strip())
        out.unlink(missing_ok=True)
        return
    out.replace(video)


@contextmanager
def recording(
    out_dir: Path = DEMO_DIR, name: str = "demo", *, video: bool = True
) -> Iterator[Page]:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(tempfile.mkdtemp(prefix="demo-video-"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=_CHROMIUM_ARGS)
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(raw_dir) if video else None,
            record_video_size=VIEWPORT if video else None,
        )
        # Headless Chromium draws no cursor: without ours a click is invisible in the video.
        context.add_init_script(f"({_LAYER_JS})({json.dumps(video)})")
        page = context.new_page()
        key = id(page)
        _started[key] = time.monotonic()

        def _loaded(_page: Page) -> None:
            _ready.setdefault(key, time.monotonic())

        page.once("load", _loaded)
        try:
            yield page
        finally:
            recorded = page.video
            context.close()
            if recorded is not None:
                out = out_dir / f"{name}.webm"
                recorded.save_as(str(out))
                if key in _ready:
                    trim_start(out, _ready[key] - _started[key])
            browser.close()
            shutil.rmtree(raw_dir, ignore_errors=True)
            _started.pop(key, None)
            _ready.pop(key, None)
