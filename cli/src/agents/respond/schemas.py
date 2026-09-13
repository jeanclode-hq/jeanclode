"""Pydantic schemas exposed by the respond agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlannerInput(BaseModel):
    """Input for ``PlannerAgent``.

    Every field is inlined into the prompt template via Jinja2.
    """

    model_config = ConfigDict(extra="ignore")

    platform: str = "github"
    repo: str = ""
    pr: str = ""
    issue: str = ""
    surface: str = "unknown"
    target_url: str = ""
    mention_body: str = ""
    mention_author: str = "unknown"
    thread_id: str = ""
    comment_id: str = ""
    pr_author: str = ""
    pr_description: str = ""
    diff: str = ""
    discussions: str = ""


class PlannerOutput(BaseModel):
    """Result of one planner run.

    ``actions_taken`` tells the workflow what the planner did.
    ``route`` and ``handle`` both mean the planner already executed the
    work itself via Bash/git/``gh``/``glab`` — Python never dispatches
    on the value. The two are combinable in one turn (e.g. ``handle`` a
    reply, then ``route`` to an existing pipeline), same ordering
    discipline as before.
    """

    model_config = ConfigDict(extra="ignore")
    actions_taken: list[Literal["route", "handle"]] = Field(min_length=1)
    summary: str = ""
