"""Config endpoint — returns frontend-needed configuration."""

from fastapi import APIRouter

from api.context import get_current_app
from api.database.llm_credentials import db_list_llm_credentials
from api.services.instance_settings import load_github_config

from .schemas import AppConfig, ProviderConfig

router = APIRouter(tags=["Config"])


def _llm_configured() -> bool:
    """True if an LLM is reachable — either via env or via at least one
    credential in the pool (ADR-010). Doesn't check staleness — this is a
    coarse "is setup complete" signal, not a dispatch-time admission check."""
    app = get_current_app()
    claude_opts = app.options.claude_code
    if claude_opts.oauth_token or claude_opts.api_key:
        return True
    if not app.database:
        return False
    with app.database.session() as db:
        credentials = db_list_llm_credentials(db)
    return bool(credentials)


def _github_app_name() -> str | None:
    """Resolve the GitHub App name, preferring env then DB. None if neither set."""
    app = get_current_app()
    env_name = app.github.config.app.name if app.github and app.github.config.app else None
    if env_name:
        return env_name
    if not app.database:
        return None
    with app.database.session() as db:
        cfg = load_github_config(db)
    return (cfg or {}).get("name") or None


@router.get(
    "/config",
    operation_id="get_app_config",
    response_model=AppConfig,
)
async def get_app_config() -> AppConfig:
    """Return application configuration for the frontend.

    No authentication required — this is public configuration only
    (provider availability, install URLs, setup-needed flag).
    """
    app = get_current_app()

    # Provider availability combines env-var config with DB-stored config.
    github_enabled = bool(app.oauth and app.oauth.is_provider_enabled("github"))
    gitlab_enabled = bool(app.oauth and app.oauth.is_provider_enabled("gitlab"))
    llm_enabled = _llm_configured()

    # Build GitHub App install URL from whichever name source is set (env first,
    # then DB). Omit the URL entirely when no name is available to avoid
    # producing a malformed ``apps//installations/new`` link.
    github_app_install_url = None
    app_name = _github_app_name()
    if app_name:
        app_slug = app_name.lower().replace(" ", "-")
        github_app_install_url = f"https://github.com/apps/{app_slug}/installations/new"

    gitlab_instance_url = app.gitlab.get_effective_instance_url() if app.gitlab else None

    git_ok = github_enabled or gitlab_enabled
    return AppConfig(
        providers=ProviderConfig(
            github_enabled=github_enabled,
            gitlab_enabled=gitlab_enabled,
            llm_enabled=llm_enabled,
            github_app_install_url=github_app_install_url,
            gitlab_instance_url=gitlab_instance_url,
        ),
        setup_required=not (git_ok and llm_enabled),
        admin_enabled=app.admin_enabled,
    )
