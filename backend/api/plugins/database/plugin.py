"""Database plugin - SQLAlchemy engine management."""

import asyncio
import base64
import logging
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import anyio.to_thread
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.plugins.database.config import DatabasePluginConfig
from api.plugins.plugin import BasePlugin

logger = logging.getLogger(__name__)

# The liveness/readiness probe hits /health; its DB check must never block on
# the main connection pool. A burst of session-holding work (e.g. the k8s
# reconcile loop) can exhaust the pool, and a pooled ``engine.connect()`` would
# then wait ``pool_timeout`` seconds — long enough for the probe to time out
# and kill the pod, turning a transient load spike into a crash loop. The
# health check uses a dedicated pool-less engine with a short connect timeout
# instead, and runs off the event loop.
_HEALTH_CONNECT_TIMEOUT_SECONDS = 2
_HEALTH_CHECK_TIMEOUT_SECONDS = 3.0


class DatabasePlugin(BasePlugin[DatabasePluginConfig]):
    """Database plugin managing SQLAlchemy engine and sessions."""

    plugin_name = "database"
    config_class = DatabasePluginConfig
    priority = 10

    def __init__(self, plugin_config: DatabasePluginConfig):
        self.config = plugin_config
        self.engine: Engine | None = None
        self.SessionLocal: sessionmaker[Session] | None = None
        # Pool-less engine reserved for health probes — see the module notes.
        self._health_engine: Engine | None = None

    async def startup(self) -> None:
        """Initialize the database engine."""
        logger.info("Initializing database engine...")

        kwargs: dict[str, Any] = {"echo": self.config.echo}
        if self.config.url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_size"] = self.config.pool_size
            kwargs["max_overflow"] = self.config.max_overflow
            kwargs["pool_timeout"] = self.config.pool_timeout
            kwargs["pool_pre_ping"] = True

        self.engine = create_engine(self.config.url, **kwargs)

        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
        )

        if not self.config.url.startswith("sqlite"):
            self._health_engine = create_engine(
                self.config.url,
                echo=False,
                poolclass=NullPool,
                connect_args={"connect_timeout": _HEALTH_CONNECT_TIMEOUT_SECONDS},
            )

        self._cap_worker_threads_to_pool()
        logger.info("Database engine initialized")

    def _cap_worker_threads_to_pool(self) -> None:
        """Narrow the shared worker-thread limiter to what the pool can serve.

        anyio defaults to 40 threads, and nearly every one of them here ends
        up in SQLAlchemy — FastAPI runs each ``def`` handler on this limiter,
        and so does ``run_in_session``. Letting more threads run than the pool
        has connections does not buy throughput; it just moves the queue into
        ``pool_timeout``, where waiting ends in a 500 rather than a turn. Only
        ever narrows: a pool wider than the default keeps the default.
        """
        capacity = self.config.pool_size + self.config.max_overflow
        limiter = anyio.to_thread.current_default_thread_limiter()
        if capacity < limiter.total_tokens:
            logger.info("Capping worker threads at %d to match the connection pool", capacity)
            limiter.total_tokens = capacity

    async def shutdown(self) -> None:
        """Dispose the database engine."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
            self.SessionLocal = None
            logger.info("Database engine disposed")
        if self._health_engine:
            self._health_engine.dispose()
            self._health_engine = None

    def get_session(self) -> Session:
        """Get a new database session."""
        if not self.SessionLocal:
            raise RuntimeError("DatabasePlugin not started")
        return self.SessionLocal()

    @contextmanager
    def session(self) -> Generator[Session]:
        """Context manager for database sessions."""
        db = self.get_session()
        try:
            yield db
        finally:
            db.close()

    async def run_in_session[T](self, fn: Callable[[Session], T]) -> T:
        """Await ``fn(db)`` on a worker thread, against a session of its own.

        The codebase talks to SQLAlchemy synchronously, so running a query
        straight from a coroutine blocks the event loop — including the pool
        checkout, which waits ``pool_timeout``. That freeze stalls every other
        request in the process and the health probe with it, so async callers
        hand their DB work here instead. The thread comes from the same
        limiter that ``_cap_worker_threads_to_pool`` sizes against the pool.
        """

        def _run() -> T:
            with self.session() as db:
                return fn(db)

        return await anyio.to_thread.run_sync(_run)

    def get_encryption_key(self) -> str | None:
        """Get the encryption key for securing sensitive data.

        Returns:
            Base64url-encoded 256-bit encryption key, or None if not configured
        """
        return self.config.encryption_key

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string using AES-256-GCM.

        Args:
            plaintext: String to encrypt

        Returns:
            Base64-encoded encrypted string (nonce + ciphertext)

        Raises:
            RuntimeError: If encryption key not configured
        """
        key = self.config.encryption_key
        if not key:
            raise RuntimeError("Encryption key not configured")

        key_bytes = base64.urlsafe_b64decode(key)
        aesgcm = AESGCM(key_bytes)

        # Generate random nonce (12 bytes for GCM)
        nonce = os.urandom(12)

        # Encrypt
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

        # Return nonce + ciphertext as base64
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")

    def decrypt(self, encrypted: str) -> str:
        """Decrypt an AES-256-GCM encrypted string.

        Args:
            encrypted: Base64-encoded encrypted string (nonce + ciphertext)

        Returns:
            Decrypted plaintext string

        Raises:
            RuntimeError: If encryption key not configured
        """
        key = self.config.encryption_key
        if not key:
            raise RuntimeError("Encryption key not configured")

        key_bytes = base64.urlsafe_b64decode(key)
        aesgcm = AESGCM(key_bytes)

        # Decode the encrypted data
        data = base64.urlsafe_b64decode(encrypted)

        # Split nonce and ciphertext (nonce is first 12 bytes)
        nonce = data[:12]
        ciphertext = data[12:]

        # Decrypt
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")

    async def health_check(self) -> dict[str, Any]:
        """Check database health by executing SELECT 1.

        Runs off the event loop against the pool-less health engine with a
        hard timeout, so an exhausted main pool can never make ``/health``
        hang long enough to trip the liveness probe.
        """
        engine = self._health_engine or self.engine
        if not engine:
            return {"healthy": False, "error": "Engine not initialized"}
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._run_health_probe, engine),
                timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
            )
            return {"healthy": True}
        except TimeoutError:
            return {"healthy": False, "error": "health check timed out"}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    @staticmethod
    def _run_health_probe(engine: Engine) -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
