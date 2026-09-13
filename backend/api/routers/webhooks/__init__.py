"""Webhooks router package.

Combines Sentry, GitHub, and GitLab webhook routers.
"""

from fastapi import APIRouter

from .github import router as github_router
from .gitlab import router as gitlab_router
from .sentry.route import router as sentry_router

# Create main webhooks router
router = APIRouter(tags=["Webhooks"])

# Sentry router has full path /webhooks/sentry already
router.include_router(sentry_router)

# GitHub router has prefix /github, so mount under /webhooks
router.include_router(github_router, prefix="/webhooks")

# GitLab router has prefix /gitlab, so mount under /webhooks
router.include_router(gitlab_router, prefix="/webhooks")

__all__ = ["router"]
