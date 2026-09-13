"""GitLab API response schemas."""

from enum import StrEnum


class GitlabTokenType(StrEnum):
    """Types of GitLab tokens."""

    PERSONAL_ACCESS_TOKEN = "personal_access_token"
    PROJECT_ACCESS_TOKEN = "project_access_token"
    GROUP_ACCESS_TOKEN = "group_access_token"
