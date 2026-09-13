"""Pydantic schemas exposed by the workflows layer."""

from typing import Literal

from pydantic import BaseModel, Field


class WorkflowResult(BaseModel):
    """The terminal value of a workflow run."""

    status: Literal["success", "error"] = "success"
    summary: str = ""
    data: dict[str, object] = Field(default_factory=dict)
