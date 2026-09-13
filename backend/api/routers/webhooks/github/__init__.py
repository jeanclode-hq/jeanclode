"""GitHub webhook router package."""

from api.routers.webhooks.github.consumer import router as consumer_router
from api.routers.webhooks.github.issue_consumer import router as issue_consumer_router
from api.routers.webhooks.github.pr_consumer import router as pr_consumer_router
from api.routers.webhooks.github.route import router

__all__ = ["consumer_router", "issue_consumer_router", "pr_consumer_router", "router"]
