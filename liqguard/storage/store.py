"""`EncryptedStorage`: one entry point that owns the database, the key manager and the repositories."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from cryptography.fernet import InvalidToken

from liqguard.models import MAINNET, ExchangeBinding, User, UserThreshold

from .crypto import (
    DEFAULT_KEY_FILE,
    EncryptionKeyError,
    KeyManager,
    decrypt_with,
    encrypt_with,
    generate_key,
    resolve_master_keys,
)
from .database import SCHEMA, SCHEMA_VERSION, Database
from .repositories import (
    AuditRepository,
    BindingRepository,
    LoginTokenRepository,
    SqliteAuditSink,
    UserKeyring,
    UserRepository,
)

logger = logging.getLogger(__name__)


class EncryptedStorage:
    """
    Persistence for users, encrypted exchange bindings, audit log and login tokens.

    Key handling is envelope encryption (see `liqguard.storage.crypto`).
    """

    def __init__(
        self,
        db_path: str = "liqguard.db",
        encryption_key: Optional[str] = None,
        key_file_path: Optional[Path] = None,
    ):
        self.key_file_path = Path(key_file_path or DEFAULT_KEY_FILE)
        master_keys, self._key_source = resolve_master_keys(encryption_key, self.key_file_path)
        self.keys = KeyManager(master_keys)
        del master_keys

        self.db = Database(db_path)
        self.keyring = UserKeyring(self.db, self.keys)
        self.users = UserRepository(self.db)
        self.bindings = BindingRepository(self.db, self.keyring)
        self.audit = AuditRepository(self.db)
        self.login_tokens = LoginTokenRepository(self.db)
        self._initialized = False

    def __repr__(self) -> str:
        return f"EncryptedStorage(db={str(self.db.path)!r}, key_source={self._key_source!r})"

    @property
    def db_path(self) -> Path:
        return self.db.path

    @property
    def key_source(self) -> str:
        """One of 'provided', 'env', 'file', 'file_created'."""
        return self._key_source

    @property
    def is_key_persistent(self) -> bool:
        return self._key_source in ("env", "provided", "file", "file_created")

    @staticmethod
    def generate_key() -> str:
        return generate_key()

    def audit_sink(self) -> SqliteAuditSink:
        return SqliteAuditSink(self.db.path)

    # ------------------------------------------------------------- schema

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self.db.connect() as conn:
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                tables = {row["name"] for row in await cur.fetchall()}
            if "bindings" in tables and "api_key" in await self.db.columns(conn, "bindings"):
                await conn.execute("ALTER TABLE bindings RENAME TO bindings_legacy")
                tables.add("bindings_legacy")
            if "users" in tables and "session_epoch" not in await self.db.columns(conn, "users"):
                await conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
            await conn.executescript(SCHEMA)
            if "bindings_legacy" in tables:
                await self._migrate_legacy_bindings(conn)
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await conn.commit()
        self.db.restrict_permissions()
        self._initialized = True
        logger.info("Database ready at %s (schema v%s)", self.db.path, SCHEMA_VERSION)

    async def _migrate_legacy_bindings(self, conn) -> None:
        """Move plaintext API keys / master-key-encrypted secrets to per-user envelope encryption."""
        async with conn.execute("SELECT * FROM bindings_legacy") as cur:
            rows = await cur.fetchall()
        migrated = 0
        for row in rows:
            try:
                secret = self.keys.decrypt_legacy(row["api_secret_encrypted"]) if row["api_secret_encrypted"] else None
            except EncryptionKeyError:
                logger.error("Legacy %s binding of user %s is unreadable; user must re-bind", row["venue"], row["telegram_id"])
                continue
            dek = await self.keyring.get(conn, row["telegram_id"], create=True)
            await conn.execute(
                """
                INSERT OR REPLACE INTO bindings (
                    telegram_id, venue, network, binding_type, api_key_encrypted, api_secret_encrypted,
                    wallet_address_encrypted, permissions_json, is_active, created_at, last_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["telegram_id"], row["venue"], MAINNET, row["binding_type"],
                    encrypt_with(dek, row["api_key"]), encrypt_with(dek, secret),
                    encrypt_with(dek, row["wallet_address"]), row["permissions_json"] or "[]",
                    row["is_active"], row["created_at"], row["last_used"],
                ),
            )
            migrated += 1
        await conn.execute("DROP TABLE bindings_legacy")
        logger.warning("Migrated %d legacy binding(s) to per-user envelope encryption", migrated)

    async def rotate_master_key(self) -> int:
        """Re-wrap all data keys with the primary master key (call after prepending a new key)."""
        await self.initialize()
        return await self.keyring.rewrap_all()

    # ------------------------------------------------------------- users & bindings

    async def get_user(self, telegram_id: int) -> Optional[User]:
        await self.initialize()
        user = await self.users.get(telegram_id)
        if user:
            user.bindings = await self.bindings.list_for_user(telegram_id)
        return user

    async def save_user(self, user: User) -> None:
        await self.initialize()
        await self.users.save(user)

    async def save_binding(self, telegram_id: int, binding: ExchangeBinding) -> None:
        await self.initialize()
        await self.bindings.save(telegram_id, binding)

    async def delete_binding(self, telegram_id: int, venue: str, network: Optional[str] = None) -> bool:
        await self.initialize()
        return await self.bindings.delete(telegram_id, venue, network)

    async def update_threshold(self, telegram_id: int, threshold: UserThreshold) -> None:
        await self.initialize()
        await self.users.update_threshold(telegram_id, threshold)

    async def set_demo_mode(self, telegram_id: int, enabled: bool) -> None:
        await self.initialize()
        await self.users.set_demo_mode(telegram_id, enabled)

    async def get_all_users_with_bindings(self) -> list[User]:
        await self.initialize()
        users = []
        for telegram_id in await self.users.ids_with_active_bindings():
            user = await self.get_user(telegram_id)
            if user:
                users.append(user)
        return users

    async def get_session_epoch(self, telegram_id: int) -> Optional[int]:
        await self.initialize()
        return await self.users.get_session_epoch(telegram_id)

    async def bump_session_epoch(self, telegram_id: int) -> int:
        """Invalidate every dashboard session and unused login link of this user."""
        await self.initialize()
        await self.login_tokens.revoke_all(telegram_id)
        return await self.users.bump_session_epoch(telegram_id)

    async def list_audit(self, telegram_id: int, limit: int = 100) -> list[dict]:
        await self.initialize()
        return await self.audit.list_for_user(telegram_id, limit)

    # ------------------------------------------------------------- generic per-user encryption

    async def encrypt_for_user(self, telegram_id: int, plaintext: str) -> str:
        await self.initialize()
        async with self.db.connect() as conn:
            dek = await self.keyring.get(conn, telegram_id, create=True)
            await conn.commit()
        return encrypt_with(dek, plaintext)

    async def decrypt_for_user(self, telegram_id: int, token: str) -> str:
        """Raises EncryptionKeyError if `token` was not encrypted for this user."""
        await self.initialize()
        async with self.db.connect() as conn:
            dek = await self.keyring.get(conn, telegram_id)
        if dek is None:
            raise EncryptionKeyError("User has no data key")
        try:
            return decrypt_with(dek, token)
        except InvalidToken as e:
            raise EncryptionKeyError("Ciphertext does not belong to this user") from e

    async def close(self) -> None:
        """Connections are per-operation; nothing to close."""
