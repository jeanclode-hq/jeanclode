from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SummarizerOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""


class ParserOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""


class FileSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    summary: str = ""


class FileSummarizerOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    files: list[FileSummary] = []
