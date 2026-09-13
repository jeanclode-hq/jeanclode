"""Internal router package — container-initiated backend endpoints.

Not reachable by frontend/user-session auth (see
``api.routers.internal.dependencies``). Composes every ``/internal/*``
namespace; currently just agent memory storage.
"""

from fastapi import APIRouter

from api.routers.internal.memory import router as memory_router

router = APIRouter()
router.include_router(memory_router)

__all__ = ["router"]
