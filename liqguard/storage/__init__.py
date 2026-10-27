"""Persistence: SQLite repositories scoped by user, with per-user envelope encryption."""

from .crypto import EncryptionKeyError, KeyManager, generate_key, resolve_master_keys
from .repositories import (
    AuditRepository,
    BindingRepository,
    LoginTokenRepository,
    SqliteAuditSink,
    UserRepository,
)
from .store import EncryptedStorage

__all__ = [
    "AuditRepository",
    "BindingRepository",
    "EncryptedStorage",
    "EncryptionKeyError",
    "KeyManager",
    "LoginTokenRepository",
    "SqliteAuditSink",
    "UserRepository",
    "generate_key",
    "resolve_master_keys",
]
