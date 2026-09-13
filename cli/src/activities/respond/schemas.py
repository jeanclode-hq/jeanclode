"""Pydantic schemas for the respond activities."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.runtime.bots import login_is_bot


class MentionContext(BaseModel):
    """The mention-side context the adaptor preflight wrote under .context/."""

    model_config = ConfigDict(extra="ignore")
    platform: Literal["github", "gitlab"]
    repo: str
    pr: str = ""
    issue: str = ""
    surface: str = "unknown"
    target_url: str = ""
    mention_body: str = ""
    mention_author: str = "unknown"
    thread_id: str = ""
    comment_id: str = ""
    pr_author: str = ""

    @property
    def author_is_bot(self) -> bool:
        """True when the mentioned PR/MR was opened by a bot/token account.

        Respond fires on any human's ``@jeanclode-bot`` mention, so this
        is what keeps the ready notice off a human-authored MR where
        someone happened to ask the bot to resolve a thread.
        """
        return login_is_bot(self.pr_author, self.platform)


class RelabelPRResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    relabeled: bool = False
    error: str = ""
