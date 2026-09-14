from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FileSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    summary: str = ""


class SummaryOutput(BaseModel):
    """Output of both the summarizer and the file summarizer.

    One schema on purpose: it becomes the structured-output tool definition,
    which sits ahead of the prompt in the request, so two schemas would stop
    the file summarizer reading the summarizer's prompt cache.
    """

    model_config = ConfigDict(extra="ignore")

    description: str = ""
    files: list[FileSummary] = []


class ParserOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""
