"""Base service abstraction with lifecycle hooks.

Services are low-level infrastructure components (HTTP clients, database pools,
Redis connections, etc.) that are used internally by plugins but never exposed
directly on the Application instance.
"""

from abc import ABC, abstractmethod


class BaseService(ABC):
    """Base service with lifecycle management.

    Services handle low-level infrastructure concerns like connections,
    clients, and resource management. They are private to plugins.
    """

    @abstractmethod
    async def on_start(self) -> None:
        """Called when service starts."""
        pass

    @abstractmethod
    async def on_shutdown(self) -> None:
        """Called when service stops."""
        pass
