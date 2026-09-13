"""Pull requests router package."""

from fastapi import APIRouter

from api.routers.pull_requests.route import actions_router
from api.routers.pull_requests.route import router as list_router

# Single ``router`` symbol so the web plugin auto-loader picks both up.
# The list endpoint stays workspace-scoped at
# ``/workspaces/{workspace_id}/pull-requests`` while the action endpoints
# (manual review/summary trigger) live at ``/pull-requests/{pr_id}/...``.
router = APIRouter()
router.include_router(list_router)
router.include_router(actions_router)

__all__ = ["actions_router", "list_router", "router"]
