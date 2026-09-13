"""Base HTTP plugin for integrations that use HTTP clients.

IMPORTANT: This is a BASE CLASS, not a standalone plugin.
It provides shared HTTP client functionality for plugins like Sentry.

Do not register BaseHttpPlugin in BUILTIN_PLUGINS in app.py.
Instead, enable specific plugins (sentry, etc.) that inherit from this class.
"""

from abc import abstractmethod
from typing import TypeVar

from api.plugins.httpx.service import HttpService
from api.plugins.plugin import BasePlugin

ConfigT = TypeVar("ConfigT")


class BaseHttpPlugin(BasePlugin[ConfigT]):
    """Base plugin for HTTP-based integrations.

    Provides shared HTTP client functionality via HttpService.
    Subclasses must implement _get_base_url() and _get_default_headers().
    """

    def __init__(self, plugin_config: ConfigT):
        self.config: ConfigT = plugin_config
        self._http = HttpService(
            base_url=self._get_base_url(),
            headers=self._get_default_headers(),
        )

    @abstractmethod
    def _get_base_url(self) -> str:
        """Get the base URL for API requests."""
        pass

    @abstractmethod
    def _get_default_headers(self) -> dict[str, str]:
        """Get default headers for API requests."""
        pass

    async def startup(self) -> None:
        """Start HTTP service."""
        await self._http.on_start()

    async def shutdown(self) -> None:
        """Shutdown the HTTP service."""
        await self._http.on_shutdown()

    @property
    def http(self) -> HttpService:
        """Get HTTP service."""
        return self._http
