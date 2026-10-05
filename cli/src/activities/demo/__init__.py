"""Demo gate activities: the checkout's demo setup, and posting the recording."""

from src.activities.demo.block import (
    DEMO_END,
    DEMO_START,
    render_demo_block,
    render_demo_link,
    split_demo_block,
    with_demo_block,
)
from src.activities.demo.checkout import (
    BYPASS_FILE,
    DEMO_DIR,
    HINTS_FILE,
    SETUP_DIR,
    fresh_demo_dir,
    git_status,
    is_clean,
    read_setup_files,
    reset_demo_dir,
    restore_setup,
    stash_setup,
)
from src.activities.demo.post import demo_note, post_demos
from src.activities.demo.publish import link_demo, post_demo_note, publish_demo
from src.activities.demo.schemas import DemoGateState, DemoRound, DemoVerdict

__all__ = [
    "BYPASS_FILE",
    "DEMO_DIR",
    "DEMO_END",
    "DEMO_START",
    "HINTS_FILE",
    "SETUP_DIR",
    "DemoGateState",
    "DemoRound",
    "DemoVerdict",
    "demo_note",
    "fresh_demo_dir",
    "git_status",
    "is_clean",
    "link_demo",
    "post_demo_note",
    "post_demos",
    "publish_demo",
    "read_setup_files",
    "render_demo_block",
    "render_demo_link",
    "reset_demo_dir",
    "restore_setup",
    "split_demo_block",
    "stash_setup",
    "with_demo_block",
]
