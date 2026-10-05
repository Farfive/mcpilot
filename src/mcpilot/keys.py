"""Encryption key for local credential storage, kept out of MCP host configuration files.

Resolution order: an explicit environment variable, then the operating-system keychain
(macOS Keychain, Windows Credential Locker, Secret Service) through the optional
``keyring`` package. Host configs such as ``~/.claude.json`` never need the key.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

SERVICE = "mcpilot"


class KeychainUnavailable(RuntimeError):
    """The optional keyring package or an OS keychain backend is missing."""


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        raise KeychainUnavailable("Install keyring: pip install 'keyring>=25' (extra: mcpilot[keychain])") from None
    return keyring, KeyringError


def keychain_get(item: str) -> str | None:
    keyring, error = _keyring()
    try:
        return keyring.get_password(SERVICE, item)
    except error as exc:
        raise KeychainUnavailable(f"OS keychain is not available: {type(exc).__name__}") from None


def keychain_ensure(item: str) -> bool:
    """Create a Fernet key for ``item`` if missing. Returns True when a new key was stored."""
    if keychain_get(item):
        return False
    keyring, error = _keyring()
    try:
        keyring.set_password(SERVICE, item, Fernet.generate_key().decode())
    except error as exc:
        raise KeychainUnavailable(f"OS keychain refused the key: {type(exc).__name__}") from None
    return True


def keychain_delete(item: str) -> bool:
    keyring, error = _keyring()
    try:
        keyring.delete_password(SERVICE, item)
        return True
    except error:
        return False


def resolve_key(env_var: str | None, keychain_item: str | None) -> tuple[bytes | None, str]:
    """Return (key, source description). The description never contains the key."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var].encode(), f"zmienna środowiskowa {env_var}"
    if keychain_item:
        value = keychain_get(keychain_item)
        if value:
            return value.encode(), f"pęk kluczy systemu ({SERVICE}/{keychain_item})"
        return None, f"brak klucza w pęku kluczy ({SERVICE}/{keychain_item})"
    return None, "brak klucza"
