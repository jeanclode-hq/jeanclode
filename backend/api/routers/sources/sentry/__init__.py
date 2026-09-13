"""Sentry sources router package.

Composes:
- POST  /sources/sentry                  (link Sentry org to workspace)
- GET   /sources/sentry/projects         (list projects)
- POST  /sources/sentry/projects/resolve (auto-resolve mappings)
- PATCH /sources/sentry/projects/{id}    (manual mapping)
- POST  /sources/sentry/backfill         (trigger issue backfill)
"""

from fastapi import APIRouter

from api.routers.sources.sentry.backfill import router as backfill_router
from api.routers.sources.sentry.projects import router as projects_router
from api.routers.sources.sentry.route import router as source_router

router = APIRouter()
router.include_router(source_router)
router.include_router(projects_router, prefix="/sources/sentry")
router.include_router(backfill_router, prefix="/sources/sentry")

__all__ = ["router"]
