"""Persistence: SQLite repositories scoped by user, with per-user envelope encryption."""

from .crypto import EncryptionKeyError, KeyManager, generate_key, resolve_master_keys
from .repositories import (
    AgentActionRepository,
    AuditRepository,
    BindingRepository,
    LoginTokenRepository,
    PaymentEventRepository,
    RescuePassRepository,
    SqliteAuditSink,
    SubscriptionRepository,
    UserRepository,
)
from .store import EncryptedStorage

__all__ = [
    "AgentActionRepository",
    "AuditRepository",
    "BindingRepository",
    "EncryptedStorage",
    "EncryptionKeyError",
    "KeyManager",
    "LoginTokenRepository",
    "PaymentEventRepository",
    "RescuePassRepository",
    "SqliteAuditSink",
    "SubscriptionRepository",
    "UserRepository",
    "generate_key",
    "resolve_master_keys",
]
