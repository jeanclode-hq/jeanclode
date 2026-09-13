"""Test fixtures for the application."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from api.app import Application
from api.context import clear_current_app
from config import BackendConfig

STATIC_DIR = Path(__file__).parent.parent / "static"
TEST_CONFIG_PATH = STATIC_DIR / "test_config.yaml"


def _worker_scoped_database_url(base_url: str) -> str:
    """Give each ``pytest-xdist`` worker its own database.

    ``db_session`` (see ``tests/conftest.py``) does a full ``DROP SCHEMA
    public CASCADE`` / recreate per test — fine serially, but under
    ``pytest -n auto`` every worker doing that against the *same* database
    fights over the same schema lock instead of actually running in
    parallel. ``PYTEST_XDIST_WORKER`` (e.g. ``"gw0"``) is set by
    pytest-xdist inside each worker process and is absent for a plain
    ``pytest`` run, so this is a no-op outside of ``-n``.
    """
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker_id:
        return base_url
    parsed = urlparse(base_url)
    db_name = parsed.path.lstrip("/")
    return parsed._replace(path=f"/{db_name}_test_{worker_id}").geturl()


def _ensure_database_exists(url: str) -> None:
    """Create the database ``url`` points at if it doesn't already exist.

    ``CREATE DATABASE`` can't run inside a transaction, hence the dedicated
    autocommit engine connected to the ``postgres`` maintenance database
    rather than reusing the app's own engine.
    """
    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/")
    admin_url = parsed._replace(path="/postgres").geturl()
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def test_config() -> BackendConfig:
    """Load test configuration from static YAML.

    Under ``pytest-xdist``, ``scope="session"`` is per *worker process*
    (each worker runs its own pytest session), which is exactly the
    granularity ``_worker_scoped_database_url`` needs — the per-worker
    database is created once here, not once per test.
    """
    config = BackendConfig.from_yaml_file(TEST_CONFIG_PATH)
    # ``plugins.database`` is a raw dict (validated into DatabasePluginConfig
    # later, at plugin registration) — mutate it directly.
    db_url = _worker_scoped_database_url(config.plugins.database["url"])
    config.plugins.database["url"] = db_url
    _ensure_database_exists(db_url)
    return config


@pytest.fixture
async def app(test_config: BackendConfig) -> Application:
    """Create a test Application with database and web plugins.

    FastStream is mocked to avoid needing a real Redis connection.
    """
    application = Application(test_config)

    with (
        patch("api.plugins.faststream.plugin.RedisBroker"),
        patch("api.plugins.faststream.plugin.Redis.from_pool", return_value=AsyncMock()),
    ):
        await application.register_plugins()

    yield application  # type: ignore[misc]

    await application.shutdown()
    clear_current_app()


@pytest.fixture
def client(app: Application) -> TestClient:
    """Create a test client for the web plugin."""
    web = app.web
    assert web is not None
    return TestClient(web.get_asgi_app())
