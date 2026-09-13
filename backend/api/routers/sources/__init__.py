"""Sources router package.

Combines GitLab and Sentry source routers.
"""

from fastapi import APIRouter

from .gitlab import router as gitlab_router
from .sentry import router as sentry_router

router = APIRouter()

router.include_router(gitlab_router)
router.include_router(sentry_router)

__all__ = ["router"]
