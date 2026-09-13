"""HTTP service for making HTTP requests.

This service wraps httpx.AsyncClient and provides lifecycle management.
Used by all HTTP-based plugins (Sentry, etc.).
"""

import httpx

from api.plugins.service import BaseService


class HttpService(BaseService):
    """HTTP client service with lifecycle management."""

    def __init__(
        self,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        """Initialize HTTP service.

        Args:
            base_url: Base URL for all requests (optional)
            headers: Default headers to include in all requests
            timeout: Request timeout in seconds
        """
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def on_start(self) -> None:
        """Create and initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            follow_redirects=True,
        )

    async def on_shutdown(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the underlying HTTP client.

        Raises:
            RuntimeError: If service not started.
        """
        if not self._client:
            raise RuntimeError("HttpService not started - call on_start() first")
        return self._client

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        """Make GET request."""
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        """Make POST request."""
        return await self.client.post(url, **kwargs)

    async def put(self, url: str, **kwargs: object) -> httpx.Response:
        """Make PUT request."""
        return await self.client.put(url, **kwargs)

    async def patch(self, url: str, **kwargs: object) -> httpx.Response:
        """Make PATCH request."""
        return await self.client.patch(url, **kwargs)

    async def delete(self, url: str, **kwargs: object) -> httpx.Response:
        """Make DELETE request."""
        return await self.client.delete(url, **kwargs)
