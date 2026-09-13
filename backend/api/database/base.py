"""SQLAlchemy database base configuration."""

from collections.abc import Callable, Generator

from sqlalchemy.orm import Session

from api.context import get_current_app
from api.models.base import Base


def get_session() -> Generator[Session]:
    """Get a database session (FastAPI dependency) for a synchronous handler.

    Only for ``def`` handlers. The session holds a pooled connection from its
    first query until the request ends, which is fine when the handler is a
    straight line of DB work — and not fine in an ``async def``, where every
    ``await`` in between (a GitHub call, a container launch) would keep that
    connection checked out for the whole round trip. Async callers use
    ``run_in_session`` instead.
    """
    app = get_current_app()
    if not app.database:
        raise RuntimeError("Database plugin not enabled")

    db = app.database.get_session()
    try:
        yield db
    finally:
        db.close()


async def run_in_session[T](fn: Callable[[Session], T]) -> T:
    """Await ``fn(db)`` on a worker thread, with a session scoped to that call.

    The unit of work for async code: the pooled connection is taken when
    ``fn`` starts and returned when it ends, so it is never held across an
    ``await``, and the blocking SQLAlchemy calls never run on the event loop.

    Return plain data or response models from ``fn``. ORM instances handed
    back outlive their session and raise ``DetachedInstanceError`` on the
    first attribute the caller touches.
    """
    app = get_current_app()
    if not app.database:
        raise RuntimeError("Database plugin not enabled")

    return await app.database.run_in_session(fn)


__all__ = ["Base", "get_session", "run_in_session"]
