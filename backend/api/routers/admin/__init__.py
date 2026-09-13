"""Admin router — gated by ADMIN_SECRET, manages instance settings."""

from .route import router

__all__ = ["router"]
