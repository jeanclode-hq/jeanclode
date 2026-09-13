"""Filesystem helpers for resolving the legacy plugin tree.

The plugins under ``plugins/`` are still being migrated to the
Python-driven workflow shape one issue at a time. Their deterministic
scripts (``filter-and-route.py``, ``open-draft-pr.py``, etc.) are still
used by the script-level tests until the corresponding workflow lands,
so we keep this small resolver around.
"""

from pathlib import Path


def resolve_plugin(plugin_name: str) -> Path:
    """Return the on-disk root of a plugin (``plugins/<name>/``)."""
    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    candidates = [
        plugins_dir / plugin_name,
        Path(f"/app/plugins/{plugin_name}"),
    ]
    for candidate in candidates:
        if (candidate / ".claude-plugin" / "plugin.json").is_file():
            return candidate

    msg = f"Could not find plugin '{plugin_name}'. Expected in plugins/{plugin_name}/"
    raise FileNotFoundError(msg)
