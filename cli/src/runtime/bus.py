"""EventBus — synchronous pub/sub for typed run events."""

import contextlib
import logging
from collections.abc import Callable

from src.runtime.events import Event

logger = logging.getLogger(__name__)

type Handler = Callable[[Event], None]


class EventBus:
    """Tiny in-process pub/sub.

    `publish` fans an event out to every subscribed handler. Handler
    exceptions are swallowed and logged so a noisy subscriber can't kill
    a workflow run.
    """

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:
                logger.exception("event handler raised on %s", event.kind)
