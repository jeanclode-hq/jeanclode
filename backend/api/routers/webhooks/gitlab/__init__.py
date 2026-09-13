"""GitLab webhook router package."""

from api.routers.webhooks.gitlab.consumer import router as consumer_router
from api.routers.webhooks.gitlab.hook_consumer import router as hook_consumer_router
from api.routers.webhooks.gitlab.route import router

__all__ = ["consumer_router", "hook_consumer_router", "router"]
