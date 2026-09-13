"""Shared SSE (Server-Sent Events) infrastructure.

Provides reusable components for implementing SSE endpoints.
"""

from .manager import SHUTDOWN_SENTINEL, SSEResourceManager, sse_manager
from .schemas import EventType
from .streaming import make_sse_event

__all__ = [
    "SHUTDOWN_SENTINEL",
    "SSEResourceManager",
    "sse_manager",
    "EventType",
    "make_sse_event",
]
