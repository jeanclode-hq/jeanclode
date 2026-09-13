"""Schemas for the executions router."""

from pydantic import BaseModel


class CancelExecutionResponse(BaseModel):
    """Response for POST /executions/{execution_id}/cancel."""

    execution_id: str
    cancelled: bool
