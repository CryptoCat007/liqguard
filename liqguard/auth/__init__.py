"""Dashboard authentication primitives (framework-free)."""

from .login_links import LoginLinkService, hash_token
from .sessions import SessionClaims, SessionSigner
from .telegram_widget import TelegramIdentity, telegram_login_hash, verify_telegram_login

__all__ = [
    "LoginLinkService",
    "SessionClaims",
    "SessionSigner",
    "TelegramIdentity",
    "hash_token",
    "telegram_login_hash",
    "verify_telegram_login",
]
