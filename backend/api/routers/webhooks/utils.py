"""Shared webhook utilities."""

from pydantic import BaseModel, Field


class WebhookResponse(BaseModel):
    """Standard webhook response."""

    message: str = Field(description="Human-readable status message")
    processed: bool = Field(description="Whether the event was processed")
