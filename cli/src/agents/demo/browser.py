"""Record a demo in headless Chromium. The demo agent's scripts import this:

    from src.agents.demo.browser import recording

    with recording() as page:
        page.goto("http://localhost:5173/orders")
        page.screenshot(path="/tmp/jeanclode-demo/orders.png")

The video lands at ``<out_dir>/demo.webm`` once the block exits.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from src.activities.demo.checkout import DEMO_DIR

VIEWPORT = {"width": 1280, "height": 720}

# Only localhost resolves, and nothing goes through the egress proxy: whatever
# the app calls outside it has to be mocked (`page.route`) or it fails visibly.
_CHROMIUM_ARGS = [
    "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
]


@contextmanager
def recording(out_dir: Path = DEMO_DIR, name: str = "demo") -> Iterator[Page]:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(tempfile.mkdtemp(prefix="demo-video-"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=_CHROMIUM_ARGS)
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(raw_dir),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        try:
            yield page
        finally:
            video = page.video
            context.close()
            if video is not None:
                video.save_as(str(out_dir / f"{name}.webm"))
            browser.close()
            shutil.rmtree(raw_dir, ignore_errors=True)
