"""Configuration management for Jeanclode backend."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from options import Options


def env_constructor(loader: yaml.Loader, node: yaml.ScalarNode) -> str:
    """YAML constructor for !ENV tag to expand environment variables.

    Supports:
    - !ENV ${VAR} - Required variable (replaced with empty string if not set)
    - !ENV ${VAR:-default} - Optional with default value
    """
    value = loader.construct_scalar(node)
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(3)
        env_value = os.getenv(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        return ""

    return pattern.sub(replacer, value)


# Register the !ENV tag constructor
yaml.add_constructor("!ENV", env_constructor, Loader=yaml.SafeLoader)  # type: ignore[arg-type]


class PluginsConfig(BaseModel):
    """Plugins configuration - stores raw dict configs."""

    model_config = ConfigDict(extra="allow")

    web: dict[str, Any] = Field(default_factory=dict)
    faststream: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)
    container: dict[str, Any] = Field(default_factory=dict)
    sentry: dict[str, Any] = Field(default_factory=dict)
    github: dict[str, Any] = Field(default_factory=dict)
    gitlab: dict[str, Any] = Field(default_factory=dict)
    oauth: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)

    def get_raw_config(self, plugin_name: str) -> dict[str, Any]:
        """Get raw config dict for a plugin by name."""
        if plugin_name in self.model_fields:
            return getattr(self, plugin_name)
        return (self.model_extra or {}).get(plugin_name, {})


class BackendConfig(BaseModel):
    """Complete backend configuration."""

    app: str = "jeanclode-backend"
    environment: Literal["development", "staging", "production"]

    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    options: Options = Field(default_factory=Options)

    _config_path: str | None = None

    @property
    def config_path(self) -> str | None:
        return self._config_path

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @classmethod
    def from_yaml(cls, config_path: str) -> BackendConfig:
        """Load configuration from YAML file."""
        return cls.from_yaml_file(config_path)

    @classmethod
    def from_yaml_file(cls, config_path: Path | str) -> BackendConfig:
        """Load configuration from a specific YAML file path."""
        config_file = Path(config_path)

        if not config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")

        with Path.open(config_file) as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty configuration file: {config_file}")

        instance = cls(**data)
        instance._config_path = str(config_file.absolute())
        return instance
