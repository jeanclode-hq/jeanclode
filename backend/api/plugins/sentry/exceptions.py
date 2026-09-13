"""Custom exceptions for the Sentry API plugin."""


class SentryAPIError(Exception):
    """Base exception for Sentry API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class SentryAuthError(SentryAPIError):
    """Raised on 401/403 authentication or authorization failures."""


class SentryNotFoundError(SentryAPIError):
    """Raised when a requested resource is not found (404)."""


class SentryRateLimitError(SentryAPIError):
    """Raised when rate limit is exceeded and all retries are exhausted."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message, status_code=429)
