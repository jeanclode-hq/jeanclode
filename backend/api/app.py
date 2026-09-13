"""Application class - the core container for plugins."""

import asyncio
import importlib.metadata
import logging
import signal
from typing import Any, Self

from api.context import set_current_app
from api.plugins.container.plugin import ContainerPlugin
from api.plugins.database.plugin import DatabasePlugin
from api.plugins.faststream.plugin import FastStreamPlugin
from api.plugins.github.plugin import GitHubPlugin
from api.plugins.gitlab.plugin import GitLabPlugin
from api.plugins.oauth.plugin import OAuthPlugin
from api.plugins.plugin import BasePlugin
from api.plugins.sentry.plugin import SentryPlugin
from api.plugins.web.plugin import WebServerPlugin
from config import BackendConfig

logger = logging.getLogger(__name__)

BUILTIN_PLUGINS: list[type[BasePlugin]] = [
    DatabasePlugin,
    GitHubPlugin,
    GitLabPlugin,
    OAuthPlugin,
    WebServerPlugin,
    FastStreamPlugin,
    ContainerPlugin,
    SentryPlugin,
]


class Application:
    """Main application container."""

    def __init__(self, config: BackendConfig):
        self._backend_config = config
        self._plugins: list[BasePlugin] = []
        self._admin_enabled: bool = True

    @property
    def database(self) -> DatabasePlugin | None:
        """Get database plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, DatabasePlugin)), None)

    @property
    def web(self) -> WebServerPlugin | None:
        """Get web server plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, WebServerPlugin)), None)

    @property
    def faststream(self) -> FastStreamPlugin | None:
        """Get FastStream plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, FastStreamPlugin)), None)

    @property
    def container(self) -> ContainerPlugin | None:
        """Get container plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, ContainerPlugin)), None)

    @property
    def sentry(self) -> SentryPlugin | None:
        """Get Sentry plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, SentryPlugin)), None)

    @property
    def github(self) -> GitHubPlugin | None:
        """Get GitHub plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, GitHubPlugin)), None)

    @property
    def gitlab(self) -> GitLabPlugin | None:
        """Get GitLab plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, GitLabPlugin)), None)

    @property
    def oauth(self) -> OAuthPlugin | None:
        """Get OAuth plugin if enabled."""
        return next((p for p in self._plugins if isinstance(p, OAuthPlugin)), None)

    @property
    def options(self):
        """Get application options."""
        return self._backend_config.options

    @property
    def admin_enabled(self) -> bool:
        """Whether the admin/setup UI should be exposed.

        Computed once at startup from env-var config (see ``_compute_admin_enabled``).
        False means the instance is fully managed via env vars — DB-backed admin
        writes would be silently shadowed by env precedence, so we hide the UI.
        """
        return self._admin_enabled

    def _compute_admin_enabled(self) -> bool:
        """Admin is enabled unless *every* provider is fully env-configured.

        A git provider counts as env-configured only when all of its env vars
        are present (partial env means the admin must still fill the rest).
        LLM counts as env-configured when either a Claude Code OAuth token or
        an Anthropic API key is set.
        """
        gh = self.github.config.app if self.github else None
        github_env_complete = bool(
            gh
            and gh.client_id
            and gh.client_secret
            and gh.app_id
            and gh.webhook_secret
            and (gh.private_key_path or gh.private_key_pem)
        )

        gl = self.gitlab.config.oauth if self.gitlab else None
        gitlab_env_complete = bool(gl and gl.client_id and gl.client_secret)

        git_env_complete = github_env_complete or gitlab_env_complete

        claude_opts = self.options.claude_code
        llm_env_complete = bool(claude_opts.oauth_token or claude_opts.api_key)

        return not (git_env_complete and llm_env_complete)

    def get_plugin(self, name: str) -> BasePlugin | None:
        """Get a plugin by name."""
        return next((p for p in self._plugins if p.plugin_name == name), None)

    async def health_check(self) -> dict[str, Any]:
        """Aggregated health check across all plugins."""
        plugins: dict[str, dict[str, Any]] = {}
        all_healthy = True

        for plugin in self._plugins:
            try:
                result = await plugin.health_check()
                plugins[plugin.plugin_name] = result
                if not result.get("healthy", False):
                    all_healthy = False
            except Exception as e:
                plugins[plugin.plugin_name] = {"healthy": False, "error": str(e)}
                all_healthy = False

        return {"healthy": all_healthy, "plugins": plugins}

    async def startup(self, plugins: list[BasePlugin]) -> None:
        """Start all plugins."""
        set_current_app(self)
        self._plugins = plugins

        for plugin in self._plugins:
            plugin_name = plugin.__class__.__name__
            try:
                await plugin.startup()
                logger.info(f"Plugin started: {plugin_name}")
            except Exception as e:
                logger.error(f"Failed to start plugin {plugin_name}: {e}", exc_info=True)
                raise

        # Generate the memory API signing secret now if it doesn't exist yet,
        # so the instance is fully configured from the first dispatch. Never
        # fatal — e.g. a missing encryption key just defers this to the
        # first real memory call, which will surface the error there.
        if self.database:
            from api.services.instance_settings import get_or_create_memory_signing_secret

            try:
                with self.database.session() as db:
                    get_or_create_memory_signing_secret(db)
            except Exception:
                logger.exception("Failed to provision memory signing secret at startup")

        # Plugin configs are loaded; compute admin_enabled once and cache it.
        self._admin_enabled = self._compute_admin_enabled()
        logger.info(
            f"Admin UI {'enabled' if self._admin_enabled else 'disabled (env-managed instance)'}"
        )

        # Start watchers after all plugins are up (cross-plugin deps are ready)
        for plugin in self._plugins:
            plugin_name = plugin.__class__.__name__
            try:
                await plugin.watch()
            except Exception as e:
                logger.error(f"Failed to start watcher for {plugin_name}: {e}", exc_info=True)
                raise

    async def shutdown(self) -> None:
        """Shutdown all plugins in reverse order."""
        logger.info("Shutting down application...")

        for plugin in reversed(self._plugins):
            plugin_name = plugin.__class__.__name__
            try:
                await plugin.shutdown()
            except Exception as e:
                logger.error(f"Error stopping plugin {plugin_name}: {e}", exc_info=True)

        logger.info("Application shutdown complete")

    async def run(self) -> None:
        """Run the application. Waits for shutdown signals."""
        logger.info("Application running...")

        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def signal_handler():
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        try:
            await shutdown_event.wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.shutdown()

    async def register_plugins(self) -> None:
        """Register and start all enabled plugins from config."""
        all_plugin_classes: list[type[BasePlugin]] = list(BUILTIN_PLUGINS)

        # Discover external plugins via entry points
        for ep in importlib.metadata.entry_points(group="jeanclode.plugins"):
            try:
                plugin_class = ep.load()
                all_plugin_classes.append(plugin_class)
                logger.info(f"Discovered external plugin: {ep.name} ({plugin_class.__name__})")
            except Exception as e:
                logger.error(f"Failed to load external plugin '{ep.name}': {e}")

        # Sort by priority (lower = earlier)
        all_plugin_classes.sort(key=lambda cls: cls.priority)

        plugins = []
        for plugin_class in all_plugin_classes:
            plugin_name = plugin_class.plugin_name
            config_data = self._backend_config.plugins.get_raw_config(plugin_name)
            plugin_config = plugin_class.config_class(**config_data)

            if not plugin_config.enabled:
                logger.debug(f"Plugin '{plugin_name}' is disabled, skipping")
                continue

            plugin = plugin_class(plugin_config)
            plugins.append(plugin)

        if not plugins:
            logger.warning("No plugins enabled! Check your configuration.")

        await self.startup(plugins)

    @classmethod
    async def from_config(cls, config_path: str) -> Self:
        """Create application and register plugins from config."""
        config = BackendConfig.from_yaml(config_path)
        app = cls(config)
        await app.register_plugins()
        return app
