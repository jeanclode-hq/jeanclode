"""Demo gate state shared by the hook and the runner."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DemoVerdict = Literal["ok", "broken", "unavailable", "nothing_to_show"]


class DemoRound(BaseModel):
    verdict: DemoVerdict
    evidence: str = ""
    # The checkout the app was launched from: its PR/MR carries the video.
    app_repo: str = ""
    video_path: str = ""
    screenshots: list[str] = Field(default_factory=list)
    # `git stash show -p` of the demo's setup, keyed by worktree path.
    setup_diffs: dict[str, str] = Field(default_factory=dict)
    # HEAD of each worktree when the round ran: an `ok` stays valid until one moves.
    heads: dict[str, str] = Field(default_factory=dict)


class DemoGateState(BaseModel):
    rounds: int = 0
    last: DemoRound | None = None
    ok: DemoRound | None = None
    bypass_reason: str = ""
    # The last round's failure, when the demo agent crashed or timed out.
    error: str = ""
