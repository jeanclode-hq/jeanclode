"""Base plugin abstraction.

Plugins are the public API of the application. They implement business logic
and are the only components attached to the Application instance.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, TypeVar

ConfigT = TypeVar("ConfigT")


class BasePlugin[ConfigT](ABC):
    """Base plugin with lifecycle hooks.

    Every feature in the application is implemented as a plugin.
    Plugins can use services internally but must never expose them.
    """

    plugin_name: ClassVar[str]
    config_class: ClassVar[type]
    priority: ClassVar[int] = 100

    config: ConfigT

    def __init__(self, config: ConfigT) -> None:
        self.config = config

    @abstractmethod
    async def startup(self) -> None:
        """Initialize the plugin using self.config."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the plugin and cleanup resources."""
        pass

    async def watch(self) -> None:  # noqa: B027
        """Start watchers. Called after all plugins have started.

        Override in plugins that need to watch container lifecycle.
        This hook exists because watchers depend on cross-plugin wiring
        (e.g., FastStream broker) that isn't available during startup().
        """

    async def health_check(self) -> dict[str, Any]:
        """Check plugin health. Override for meaningful checks."""
        return {"healthy": True}
