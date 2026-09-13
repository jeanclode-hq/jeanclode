"""Tests for TOML config file operations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.config_file import (
    get_config_model,
    get_config_small_model,
    load_config_file,
    run_interactive_setup,
    save_config_file,
)


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_config_file(tmp_path / "missing.toml") is None


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".jeanclode" / "config.toml"
    data = {"models": {"model": "opus"}}
    save_config_file(data, path)

    loaded = load_config_file(path)
    assert loaded is not None
    assert loaded["models"]["model"] == "opus"


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "config.toml"
    save_config_file({"models": {"model": "haiku"}}, path)
    assert path.exists()


def test_get_config_model_extracts() -> None:
    assert get_config_model({"models": {"model": "haiku"}}) == "haiku"
    assert get_config_model({"models": {"model": "opus"}}) == "opus"


def test_get_config_model_missing() -> None:
    assert get_config_model({"models": {}}) is None


def test_get_config_model_no_models_section() -> None:
    assert get_config_model({}) is None


def test_get_config_model_custom_name_preserved() -> None:
    assert get_config_model({"models": {"model": "gpt-4"}}) == "gpt-4"


def test_get_config_small_model_extracts() -> None:
    assert get_config_small_model({"models": {"small_model": "haiku"}}) == "haiku"
    assert get_config_small_model({"models": {"small_model": "sonnet"}}) == "sonnet"


def test_get_config_small_model_missing() -> None:
    assert get_config_small_model({"models": {}}) is None


def test_get_config_small_model_no_models_section() -> None:
    assert get_config_small_model({}) is None


def test_get_config_small_model_custom_name_preserved() -> None:
    assert get_config_small_model({"models": {"small_model": "gpt-4"}}) == "gpt-4"


# Interactive setup now asks: auth(1), model(1), small_model(1) = 3 prompts for api-key
# For proxy: auth(1), base_url(1), model(1), small_model(1) = 4 prompts


@patch("src.config_file.Prompt.ask", side_effect=["1", "3", "1"])
def test_interactive_setup_api_key(mock_ask: object, tmp_path: Path) -> None:
    """Auth=api-key, model=opus(3), small_model=haiku(1)."""
    path = tmp_path / "config.toml"
    result = run_interactive_setup(path)
    assert result == {"models": {"model": "opus", "small_model": "haiku"}}

    loaded = load_config_file(path)
    assert loaded is not None
    assert loaded["models"]["model"] == "opus"
    assert loaded["models"]["small_model"] == "haiku"


@patch("src.config_file.Prompt.ask", side_effect=["2", "2", "1"])
def test_interactive_setup_subscription(mock_ask: object, tmp_path: Path) -> None:
    """Auth=subscription, model=sonnet(2), small_model=haiku(1)."""
    path = tmp_path / "config.toml"
    result = run_interactive_setup(path)
    assert result["models"]["model"] == "sonnet"
    assert result["models"]["small_model"] == "haiku"


@patch(
    "src.config_file.Prompt.ask",
    side_effect=["3", "https://proxy.example.com/v1", "my-model", "my-small-model"],
)
def test_interactive_setup_proxy(mock_ask: object, tmp_path: Path) -> None:
    """Auth=proxy, prompts for base URL, free-text model and small model."""
    path = tmp_path / "config.toml"
    result = run_interactive_setup(path)
    assert result["models"]["model"] == "my-model"
    assert result["models"]["small_model"] == "my-small-model"
    assert result["urls"]["base"] == "https://proxy.example.com/v1"

    loaded = load_config_file(path)
    assert loaded is not None
    assert loaded["urls"]["base"] == "https://proxy.example.com/v1"
