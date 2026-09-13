"""Web server plugin - FastAPI as a plugin."""

import asyncio
import importlib
import logging
import pkgutil
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from api.context import get_current_app
from api.plugins.plugin import BasePlugin
from api.plugins.web.config import WebPluginConfig
from api.plugins.web.session import SessionManager

logger = logging.getLogger(__name__)


class WebServerPlugin(BasePlugin[WebPluginConfig]):
    """Web server plugin wrapping FastAPI."""

    plugin_name = "web"
    config_class = WebPluginConfig
    priority = 50

    def __init__(self, plugin_config: WebPluginConfig):
        self.config = plugin_config
        self.app: FastAPI | None = None
        self.sessions: SessionManager = SessionManager(plugin_config.session)
        self._server_task: asyncio.Task[None] | None = None
        self._server: uvicorn.Server | None = None

    def _discover_routers(self, fastapi_app: FastAPI) -> None:
        """Scan api.routers package and include any modules with a `router` attribute."""
        import api.routers as routers_pkg

        for module_info in pkgutil.iter_modules(routers_pkg.__path__):
            if module_info.name.startswith("_") or module_info.name == "base_schema":
                continue
            try:
                module = importlib.import_module(f"api.routers.{module_info.name}")
                if hasattr(module, "router"):
                    fastapi_app.include_router(module.router)
                    logger.info(f"Included router from api.routers.{module_info.name}")
            except Exception as e:
                logger.error(f"Failed to load router api.routers.{module_info.name}: {e}")

    async def startup(self) -> None:
        """Start the FastAPI web server."""
        app = get_current_app()

        self.app = FastAPI(
            title="Jeanclode API",
            description="Autonomous Sentry error triage and fix system",
            version="0.1.0",
            docs_url="/docs",
            redoc_url="/redoc",
        )

        # Configure CORS
        allowed_origins = [self.config.frontend_url]
        if app._backend_config.is_development:
            allowed_origins.extend(self.config.cors.additional_dev_origins)

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=self.config.cors.allow_credentials,
            allow_methods=self.config.cors.allow_methods,
            allow_headers=self.config.cors.allow_headers,
        )

        # Starlette SessionMiddleware for OAuth state (PKCE code_verifier, link_mode)
        self.app.add_middleware(
            SessionMiddleware,
            secret_key=self.config.session.oauth_state_secret,
        )

        # Handle reverse proxy headers
        if self.config.behind_proxy:
            from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

            self.app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

        # Error handling middleware
        @self.app.middleware("http")
        async def error_handling_middleware(request: Request, call_next: Any) -> JSONResponse:
            try:
                return await call_next(request)  # type: ignore[return-value]
            except Exception as exc:
                logger.error(f"Unhandled exception: {exc}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "internal_server_error",
                        "message": "An unexpected error occurred",
                    },
                )

        # Health check routes
        @self.app.get("/", tags=["Health"])
        async def root() -> dict[str, str]:
            return {
                "status": "ok",
                "message": "Jeanclode API is running",
                "environment": app._backend_config.environment,
            }

        @self.app.get("/health", tags=["Health"])
        async def health() -> JSONResponse:
            result = await app.health_check()
            status_code = 200 if result["healthy"] else 503
            return JSONResponse(content=result, status_code=status_code)

        # Discover and include routers
        self._discover_routers(self.app)

        # Start uvicorn server in background task
        if not self.config.skip_server:
            self._server_task = asyncio.create_task(self._run_server())

    async def _run_server(self) -> None:
        """Run the uvicorn server."""
        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            reload=self.config.reload,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None
        try:
            print(f"Starting web server at http://{self.config.host}:{self.config.port}/")
            await self._server.serve()
        except asyncio.CancelledError:
            logger.info("Server task cancelled")
            raise

    async def shutdown(self) -> None:
        """Shutdown the web server."""
        if self._server:
            self._server.should_exit = True
            self._server.force_exit = True

        if self._server_task and not self._server_task.done():
            try:
                await asyncio.wait_for(self._server_task, timeout=2.0)
            except TimeoutError:
                logger.warning("Server shutdown timeout, forcing shutdown...")
                self._server_task.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError):
                    await self._server_task
            except asyncio.CancelledError:
                pass

        self._server = None
        self._server_task = None

    def get_asgi_app(self) -> FastAPI:
        """Get the ASGI application."""
        if not self.app:
            raise RuntimeError("WebServerPlugin not started")
        return self.app

    async def health_check(self) -> dict[str, Any]:
        """Check whether the web server is running."""
        if not self.app:
            return {"healthy": False, "error": "FastAPI app not initialized"}
        server_running = self._server_task is not None and not self._server_task.done()
        return {"healthy": True, "server_running": server_running}
