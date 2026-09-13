"""Shared concurrency gate for execution-status stream consumers.

The container reconcile loop can publish one status message per terminal Job.
FastStream runs a handler coroutine per message with no built-in cap, and each
handler opens a synchronous DB session. A burst wider than the SQLAlchemy pool
(``pool_size`` + ``max_overflow``) exhausts it, and every session-holding path
then blocks for ``pool_timeout`` seconds — including ``/health``, which is
enough to trip the liveness probe and restart the pod.

This semaphore bounds concurrent status-consumer work to comfortably below the
pool size, turning an overload into backpressure (messages wait) instead of
pool exhaustion. Sized for the github/gitlab/sentry status consumers combined.
"""

import asyncio

# DatabasePluginConfig defaults to pool_size=5 + max_overflow=10; deploys bump
# it higher. 8 leaves headroom for web requests and the reconcile's own DB work.
STATUS_CONSUMER_CONCURRENCY = 8

status_consumer_gate = asyncio.Semaphore(STATUS_CONSUMER_CONCURRENCY)
