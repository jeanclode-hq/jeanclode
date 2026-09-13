"""OAuth plugin — multi-provider OAuth client management with PKCE."""

import logging

from authlib.integrations.starlette_client import OAuth

from api.context import get_current_app
from api.plugins.oauth.config import OAuthPluginConfig
from api.plugins.plugin import BasePlugin

logger = logging.getLogger(__name__)


class OAuthPlugin(BasePlugin[OAuthPluginConfig]):
    """OAuth plugin managing OAuth provider registration.

    Two sources of provider config:

    1. Env vars (via the GitHub/GitLab plugins) — registered once at
       startup into ``self.oauth._clients``. Static for the process lifetime.
    2. Encrypted DB rows (``instance_settings``) — **not** cached. Loaded
       fresh on every ``get_client()`` call so multi-pod deployments stay
       consistent after an admin edit on a peer pod (no pub/sub needed —
       OAuth ``/authorize`` is a rare user-initiated action, so the extra
       DB query is negligible).
    """

    plugin_name = "oauth"
    config_class = OAuthPluginConfig
    priority = 30

    def __init__(self, plugin_config: OAuthPluginConfig):
        self.config = plugin_config
        self.oauth = OAuth()
        # Providers registered from env-var config at startup.
        # Anything not in this set is treated as DB-sourced and reloaded per-call.
        self._env_sourced: set[str] = set()

    async def startup(self) -> None:
        """Register env-var-configured providers. DB providers are lazy.

        A provider config block may exist in YAML with ``!ENV`` placeholders
        that resolve to empty strings when the env vars are unset. Treat
        blank ``client_id``/``client_secret`` as "not configured" so the
        setup wizard can kick in.
        """
        app = get_current_app()

        gh = app.github.config.app if app.github else None
        if gh and gh.client_id and gh.client_secret:
            self._register_github_from_config(gh)
            self._env_sourced.add("github")

        gl = app.gitlab.config.oauth if app.gitlab else None
        if gl and gl.client_id and gl.client_secret:
            self._register_gitlab_from_config(gl)
            self._env_sourced.add("gitlab")

        if not self._env_sourced:
            logger.info("No env-var OAuth providers; DB-stored providers will be loaded per-login")

    async def shutdown(self) -> None:
        self.oauth._clients.clear()
        self._env_sourced.clear()

    # --- public API --------------------------------------------------------

    def get_client(self, provider: str):  # type: ignore[no-untyped-def]
        """Return an Authlib OAuth client for the given provider.

        For env-var providers returns the cached client. For DB-sourced
        providers, reloads config from the DB, re-registers, then returns
        a fresh client — this keeps multi-pod deployments consistent.
        """
        if provider not in self._env_sourced:
            self._reload_db_provider(provider)
        return self.oauth.create_client(provider)

    def get_oauth(self) -> OAuth:
        return self.oauth

    def is_provider_enabled(self, provider: str) -> bool:
        """Return True if the provider has either env-var or DB-stored config."""
        if provider in self._env_sourced:
            return True
        return self._db_provider_has_config(provider)

    # --- internal: DB loading ---------------------------------------------

    def _db_provider_has_config(self, provider: str) -> bool:
        """Cheap check for DB-stored config existence (no decryption)."""
        app = get_current_app()
        if not app.database:
            return False
        # Import here to avoid circular imports at module load time.
        from api.database.instance_settings import db_get_settings_by_category

        try:
            with app.database.session() as db:
                return len(db_get_settings_by_category(db, provider)) > 0
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Failed to check DB for provider {provider}: {e}")
            return False

    def _reload_db_provider(self, provider: str) -> None:
        """Load DB-stored config for the provider and (re-)register with Authlib.

        If the provider has no DB config, removes it from ``oauth._clients``
        so ``is_provider_enabled`` / ``get_client`` behave correctly.
        """
        app = get_current_app()
        if not app.database:
            self.oauth._clients.pop(provider, None)
            return

        from api.services.instance_settings import load_github_config, load_gitlab_config

        with app.database.session() as db:
            if provider == "github":
                cfg = load_github_config(db)
            elif provider == "gitlab":
                cfg = load_gitlab_config(db)
            else:
                cfg = None

        if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
            self.oauth._clients.pop(provider, None)
            return

        if provider == "github":
            self._register_github_from_dict(cfg)
        elif provider == "gitlab":
            self._register_gitlab_from_dict(cfg)

    # --- internal: Authlib registration ------------------------------------

    def _register_github_from_config(self, gh) -> None:
        self.oauth.register(
            name="github",
            client_id=gh.client_id,
            client_secret=gh.client_secret,
            authorize_url=gh.authorize_url,
            access_token_url=gh.token_url,
            api_base_url=gh.api_base_url,
            client_kwargs={
                "scope": " ".join(gh.scopes),
                "code_challenge_method": "S256",
            },
        )
        logger.info("Registered GitHub OAuth provider from env (PKCE enabled)")

    def _register_github_from_dict(self, config: dict) -> None:
        scopes = config.get("scopes") or ["read:user", "user:email", "read:org"]
        if isinstance(scopes, str):
            scopes = scopes.split()
        self.oauth.register(
            name="github",
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            authorize_url=config.get("authorize_url", "https://github.com/login/oauth/authorize"),
            access_token_url=config.get("token_url", "https://github.com/login/oauth/access_token"),
            api_base_url=config.get("api_base_url", "https://api.github.com/"),
            client_kwargs={
                "scope": " ".join(scopes),
                "code_challenge_method": "S256",
            },
        )

    def _register_gitlab_from_config(self, gl) -> None:
        self.oauth.register(
            name="gitlab",
            client_id=gl.client_id,
            client_secret=gl.client_secret,
            server_metadata_url=f"{gl.instance_url}/.well-known/openid-configuration",
            api_base_url=f"{gl.instance_url}/api/v4",
            client_kwargs={
                "scope": " ".join(gl.scopes),
                "code_challenge_method": "S256",
            },
        )
        logger.info("Registered GitLab OAuth provider from env (PKCE enabled)")

    def _register_gitlab_from_dict(self, config: dict) -> None:
        instance_url = config.get("instance_url", "https://gitlab.com")
        scopes = config.get("scopes") or ["read_user", "read_api"]
        if isinstance(scopes, str):
            scopes = scopes.split()
        self.oauth.register(
            name="gitlab",
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            server_metadata_url=f"{instance_url}/.well-known/openid-configuration",
            api_base_url=f"{instance_url}/api/v4",
            client_kwargs={
                "scope": " ".join(scopes),
                "code_challenge_method": "S256",
            },
        )
