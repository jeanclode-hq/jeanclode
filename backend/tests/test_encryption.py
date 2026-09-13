"""Tests for database plugin encryption."""

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from api.plugins.database.config import DatabasePluginConfig
from api.plugins.database.plugin import DatabasePlugin


@pytest.fixture
def encryption_key() -> str:
    """Generate a valid 256-bit encryption key."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


@pytest.fixture
def db_plugin(encryption_key: str) -> DatabasePlugin:
    """Create a DatabasePlugin with encryption configured."""
    config = DatabasePluginConfig(
        enabled=True,
        url="sqlite:///:memory:",
        encryption_key=encryption_key,
    )
    return DatabasePlugin(config)


def test_encrypt_decrypt_roundtrip(db_plugin):
    """Encrypt then decrypt returns original plaintext."""
    plaintext = "my-secret-token-12345"
    encrypted = db_plugin.encrypt(plaintext)
    decrypted = db_plugin.decrypt(encrypted)
    assert decrypted == plaintext


def test_encrypted_is_different_from_plaintext(db_plugin):
    """Encrypted output is not the same as the plaintext."""
    plaintext = "my-secret-token"
    encrypted = db_plugin.encrypt(plaintext)
    assert encrypted != plaintext


def test_different_ciphertext_per_call(db_plugin):
    """Each encryption produces different ciphertext (random nonce)."""
    plaintext = "same-input"
    encrypted1 = db_plugin.encrypt(plaintext)
    encrypted2 = db_plugin.encrypt(plaintext)
    assert encrypted1 != encrypted2
    # But both decrypt to the same value
    assert db_plugin.decrypt(encrypted1) == plaintext
    assert db_plugin.decrypt(encrypted2) == plaintext


def test_wrong_key_fails():
    """Decrypting with a different key raises an error."""
    key1 = base64.urlsafe_b64encode(os.urandom(32)).decode()
    key2 = base64.urlsafe_b64encode(os.urandom(32)).decode()

    plugin1 = DatabasePlugin(
        DatabasePluginConfig(enabled=True, url="sqlite:///:memory:", encryption_key=key1)
    )
    plugin2 = DatabasePlugin(
        DatabasePluginConfig(enabled=True, url="sqlite:///:memory:", encryption_key=key2)
    )

    encrypted = plugin1.encrypt("secret")
    with pytest.raises(InvalidTag):
        plugin2.decrypt(encrypted)


def test_encrypt_raises_without_key():
    """Encryption raises RuntimeError when key is not configured."""
    plugin = DatabasePlugin(
        DatabasePluginConfig(enabled=True, url="sqlite:///:memory:", encryption_key=None)
    )
    with pytest.raises(RuntimeError, match="Encryption key not configured"):
        plugin.encrypt("test")


def test_decrypt_raises_without_key():
    """Decryption raises RuntimeError when key is not configured."""
    plugin = DatabasePlugin(
        DatabasePluginConfig(enabled=True, url="sqlite:///:memory:", encryption_key=None)
    )
    with pytest.raises(RuntimeError, match="Encryption key not configured"):
        plugin.decrypt("test")
