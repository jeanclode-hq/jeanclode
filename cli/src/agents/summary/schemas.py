from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SummarizerOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""


class ParserOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""
