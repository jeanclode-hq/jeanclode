"""Config file reader/writer for ~/.jeanclode/config.toml

Stores model preferences only (no secrets). API keys come from env vars.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

_console = Console(highlight=False)

CONFIG_PATH = Path.home() / ".jeanclode" / "config.toml"

VALID_MODELS = ("haiku", "sonnet", "opus")

MODELS = [
    ("1", "haiku"),
    ("2", "sonnet"),
    ("3", "opus"),
]

AUTH_METHODS = [
    ("1", "api-key"),
    ("2", "subscription"),
    ("3", "proxy"),
]

AUTH_ENV_VARS: dict[str, str] = {
    "api-key": "ANTHROPIC_API_KEY",
    "subscription": "CLAUDE_CODE_OAUTH_TOKEN",
}


def load_config_file(path: Path = CONFIG_PATH) -> dict | None:
    """Load config from TOML file. Returns None if file doesn't exist."""
    if not path.is_file():
        return None
    with path.open("rb") as f:
        return tomllib.load(f)


def save_config_file(data: dict, path: Path = CONFIG_PATH) -> None:
    """Write config dict to TOML file (simple key=value serialization)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        if isinstance(values, dict):
            for key, val in values.items():
                lines.append(f'{key} = "{val}"')
        lines.append("")
    path.write_text("\n".join(lines))


def get_config_model(data: dict) -> str | None:
    """Get model from config data. Returns None if not set."""
    models = data.get("models")
    if not isinstance(models, dict):
        return None
    value = models.get("model")
    return value if isinstance(value, str) and value else None


def get_config_small_model(data: dict) -> str | None:
    """Get small model from config data. Returns None if not set."""
    models = data.get("models")
    if not isinstance(models, dict):
        return None
    value = models.get("small_model")
    return value if isinstance(value, str) and value else None


def _pick_model(is_proxy: bool = False, label: str = "Model name", default: str = "sonnet") -> str:
    """Show model menu, return selected model name.

    For proxy users: free-text input with no default (may use non-Anthropic models).
    For API key / subscription: numbered menu with ``default`` as the selected entry.
    """
    if is_proxy:
        _console.print(f"  [dim]Enter the {label.lower()}.[/]")
        model = ""
        while not model:
            model = Prompt.ask(f"  {label}", console=_console).strip()
        return model

    default_key = next((k for k, name in MODELS if name == default), "2")
    _console.print(f"  [dim]We recommend [bold]{default}[/bold]. Press Enter to use it.[/]")
    for key, name in MODELS:
        suffix = " (default)" if name == default else ""
        _console.print(f"    [bold cyan]{key}[/]) {name}{suffix}")
    _console.print()

    valid = {k for k, _ in MODELS}
    choice = ""
    while choice not in valid:
        choice = Prompt.ask("  Pick a number", default=default_key, console=_console).strip()
    return next(name for k, name in MODELS if k == choice)


def _pick_auth_method() -> str:
    """Show auth method menu, return selected method."""
    _console.print("    [bold cyan]1[/]) API key (ANTHROPIC_API_KEY)")
    _console.print("    [bold cyan]2[/]) Claude Max subscription (CLAUDE_CODE_OAUTH_TOKEN)")
    _console.print("    [bold cyan]3[/]) Proxy (custom API endpoint)")
    _console.print()

    valid = {k for k, _ in AUTH_METHODS}
    choice = ""
    while choice not in valid:
        choice = Prompt.ask("  Pick a number", console=_console).strip()
    return next(name for k, name in AUTH_METHODS if k == choice)


def _prompt_base_url() -> str:
    """Prompt for the proxy base URL."""
    url = ""
    while not url:
        url = Prompt.ask("  API base URL", console=_console).strip()
    return url.rstrip("/")


def run_interactive_setup(path: Path = CONFIG_PATH) -> dict:
    """Prompt for auth + model choice. Save to config file (no secrets)."""
    _console.print()
    _console.print(
        Panel(
            "Jeanclode uses Claude to triage Sentry issues and fix bugs.\n"
            "  Choose your authentication method and model.\n\n"
            "  Available models: haiku (fast), sonnet (balanced), opus (strongest)",
            title="[bold]Welcome to Jeanclode![/]",
            expand=False,
        )
    )
    _console.print()

    # Step 1: Auth method
    _console.print("  [bold]Step 1 — Authentication:[/]")
    auth_method = _pick_auth_method()

    # Step 1b: Proxy base URL
    base_url: str | None = None
    if auth_method == "proxy":
        _console.print("\n  [bold]Step 1b — Proxy endpoint:[/]")
        base_url = _prompt_base_url()

    # Step 2: Model (high tier — reasoning-heavy work)
    is_proxy = auth_method == "proxy"
    _console.print("\n  [bold]Step 2 — Choose main model (code review, fix drafting):[/]")
    model = _pick_model(is_proxy=is_proxy, label="Model name", default="sonnet")

    # Step 3: Small model (low tier — filtering, formatting, summaries)
    _console.print("\n  [bold]Step 3 — Choose small model (deduplication, styling, summaries):[/]")
    small_model = _pick_model(is_proxy=is_proxy, label="Small model name", default="haiku")

    # Build and save config
    data: dict = {
        "models": {"model": model, "small_model": small_model},
    }
    if base_url:
        data["urls"] = {"base": base_url}
    save_config_file(data, path)

    _console.print(f"\n  [green]Saved to {path}[/]")

    # Show env var instructions based on auth method
    env_var = AUTH_ENV_VARS.get(auth_method)
    if env_var:
        _console.print("\n  [yellow]Set your API key before running:[/]")
        _console.print(f"    [dim]export {env_var}=your-key[/]")
    else:
        _console.print("\n  [yellow]Set your API key before running:[/]")
        _console.print("    [dim]export ANTHROPIC_API_KEY=your-key[/]")
    _console.print()
    return data
