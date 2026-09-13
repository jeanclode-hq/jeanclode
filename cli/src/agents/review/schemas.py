from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IssueExplorerOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    context: str = ""
    issue_refs: list[str] = Field(default_factory=list)


class FilterResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keep_indices: list[int] = Field(default_factory=list)


class StyleResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bodies: list[str] = Field(default_factory=list)
