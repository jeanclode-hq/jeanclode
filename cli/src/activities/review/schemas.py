from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.bots import login_is_bot


class GitHubComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    body: str
    line: int | None = None
    side: Literal["LEFT", "RIGHT"] = "RIGHT"


class GitLabComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    body: str
    new_path: str | None = None
    new_line: int | None = None
    old_path: str | None = None
    old_line: int | None = None


class GitHubReview(BaseModel):
    model_config = ConfigDict(extra="ignore")
    comments: list[GitHubComment] = Field(default_factory=list)


class GitLabReview(BaseModel):
    model_config = ConfigDict(extra="ignore")
    comments: list[GitLabComment] = Field(default_factory=list)


type Comment = GitHubComment | GitLabComment


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["stop", "review"]
    reason: str = ""


class UnpostedComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = ""
    line: int | None = None
    body: str
    error_type: Literal[
        "out_of_diff",
        "rate_limited",
        "permission",
        "other",
    ] = "other"
    error_detail: str = ""


class PostResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    posted: int = 0
    failed: int = 0
    lgtm: bool = False
    pr_url: str = ""
    errors: list[str] = Field(default_factory=list)
    unposted: list[UnpostedComment] = Field(default_factory=list)
    loop_triggered: bool = False


class RecoverResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recovered: int = 0
    skipped: int = 0
    pr_url: str = ""
    error: str = ""


class PRRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    platform: Literal["github", "gitlab"]
    repo: str
    pr: str
    pr_url: str = ""
    pr_author: str = ""

    @property
    def author_is_bot(self) -> bool:
        """True when the PR/MR was opened by a bot/token account, not a human.

        This gates the post-review self-mention sweep, and since the
        backend accepts a mention from any sender it is the *only* gate —
        a mention posted on a human-authored PR would dispatch.
        """
        return login_is_bot(self.pr_author, self.platform)


class PRContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: PRRef
    pr_description: str = ""
    diff: str = ""
    discussions: str = ""
