"""Enums and constants for authentication."""

from enum import StrEnum


class OAuthProvider(StrEnum):
    """OAuth provider types."""

    GITHUB = "github"
    GITLAB = "gitlab"
