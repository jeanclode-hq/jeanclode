"""Global application context for runtime state management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.app import Application

_app_context: dict[str, Application] = {}


def set_current_app(app: Application) -> None:
    """Register the application instance in global context."""
    _app_context["current_app"] = app


def get_current_app() -> Application:
    """Get the current application instance from global context."""
    app = _app_context.get("current_app")
    if app is None:
        raise RuntimeError(
            "Application not initialized. Make sure Application has been created and started."
        )
    return app


def clear_current_app() -> None:
    """Clear the application from global context."""
    _app_context.clear()
