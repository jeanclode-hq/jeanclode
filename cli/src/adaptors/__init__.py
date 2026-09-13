"""Adaptors — external service integrations for CLI pre-flight."""

from src.adaptors.registry import find_adaptor, list_commands

__all__ = ["find_adaptor", "list_commands"]
