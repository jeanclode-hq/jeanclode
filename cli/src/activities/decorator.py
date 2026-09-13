"""@activity — wrap a function so the runtime emits start/end events.

Activities are small Python units of work composed by a workflow. The
decorator handles emission so workflow authors never type the word `emit`.

Use it on either a sync or async function. The decorated function's `ctx`
must be passed as a keyword argument:

    @activity
    def push_branch(branch: str, *, ctx: RunContext) -> None: ...

    @activity
    async def open_pr(branch: str, body: str, *, ctx: RunContext) -> str: ...

Pass ``name=`` to set the user-facing label that appears in the progress
UI (otherwise the function name is used):

    @activity(name="Opening PR")
    def open_pr(...): ...
"""

import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast, overload

from src.runtime.context import RunContext
from src.runtime.events import ActivityEnd, ActivityStart


@overload
def activity[**P, R](fn: Callable[P, R]) -> Callable[P, R]: ...
@overload
def activity[**P, R](*, name: str) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def activity[**P, R](
    fn: Callable[P, R] | None = None, *, name: str | None = None
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap `fn` so its execution emits ActivityStart/ActivityEnd events.

    Works for sync and async callables. Supports both ``@activity`` (no
    args) and ``@activity(name="Display Label")`` invocation forms. The
    wrapped function's ``ctx`` keyword argument is consulted to publish
    events; it must be a ``RunContext``.
    """
    if fn is None:
        return cast(
            Callable[[Callable[P, R]], Callable[P, R]],
            functools.partial(activity, name=name),
        )

    display_name = name or fn.__name__

    if inspect.iscoroutinefunction(fn):
        async_fn = cast(Callable[..., Awaitable[Any]], fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _require_ctx(kwargs)
            ctx.emit(ActivityStart(name=display_name))
            ok = True
            started = time.monotonic()
            try:
                return await async_fn(*args, **kwargs)
            except BaseException:
                ok = False
                raise
            finally:
                ctx.emit(
                    ActivityEnd(
                        name=display_name,
                        ok=ok,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )

        return cast(Callable[P, R], async_wrapper)

    sync_fn = cast(Callable[..., Any], fn)

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = _require_ctx(kwargs)
        ctx.emit(ActivityStart(name=display_name))
        ok = True
        started = time.monotonic()
        try:
            return sync_fn(*args, **kwargs)
        except BaseException:
            ok = False
            raise
        finally:
            ctx.emit(
                ActivityEnd(
                    name=display_name,
                    ok=ok,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )

    return cast(Callable[P, R], sync_wrapper)


def _require_ctx(kwargs: dict[str, Any]) -> RunContext:
    ctx = kwargs.get("ctx")
    if not isinstance(ctx, RunContext):
        msg = "@activity requires a `ctx: RunContext` keyword argument"
        raise TypeError(msg)
    return ctx
