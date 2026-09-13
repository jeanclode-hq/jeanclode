"""Schemas for org background jobs."""

from pydantic import BaseModel, Field


class SyncOrgMembersMessage(BaseModel):
    """Message to trigger a full org member pre-population job."""

    org_id: str = Field(description="Organization database ID (UUID)")
    provider: str = Field(description="Provider: 'github' or 'gitlab'")
