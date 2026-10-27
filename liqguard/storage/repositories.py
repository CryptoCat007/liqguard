"""
Repositories. Every read and write takes the owning `telegram_id` and filters
on it; there is no query that returns another user's rows by accident.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from liqguard.models import MAINNET, ExchangeBinding, User, UserThreshold

from .crypto import KeyManager, decrypt_with, encrypt_with
from .database import Database

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class UserKeyring:
    """Per-user data keys: created on first use, stored wrapped, cached unwrapped in memory."""

    def __init__(self, db: Database, keys: KeyManager):
        self.db = db
        self.keys = keys
        self._cache: dict[int, Fernet] = {}

    def __repr__(self) -> str:
        return f"UserKeyring(cached={len(self._cache)})"

    async def get(self, conn: aiosqlite.Connection, telegram_id: int, create: bool = False) -> Optional[Fernet]:
        if telegram_id in self._cache:
            return self._cache[telegram_id]
        async with conn.execute("SELECT wrapped_dek FROM user_keys WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
        if row:
            dek = self.keys.unwrap(row["wrapped_dek"])
        elif create:
            dek, wrapped = self.keys.new_data_key()
            await conn.execute(
                "INSERT INTO user_keys (telegram_id, wrapped_dek, created_at) VALUES (?, ?, ?)",
                (telegram_id, wrapped, _now().isoformat()),
            )
        else:
            return None
        self._cache[telegram_id] = dek
        return dek

    def forget(self, telegram_id: int) -> None:
        self._cache.pop(telegram_id, None)

    async def rewrap_all(self) -> int:
        """Re-encrypt every wrapped DEK under the primary master key."""
        async with self.db.connect() as conn:
            async with conn.execute("SELECT telegram_id, wrapped_dek FROM user_keys") as cur:
                rows = await cur.fetchall()
            for row in rows:
                await conn.execute(
                    "UPDATE user_keys SET wrapped_dek = ? WHERE telegram_id = ?",
                    (self.keys.rewrap(row["wrapped_dek"]), row["telegram_id"]),
                )
            await conn.commit()
        return len(rows)


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row_to_user(row) -> User:
        threshold = UserThreshold()
        if row["threshold_json"]:
            try:
                threshold = UserThreshold.from_dict(json.loads(row["threshold_json"]))
            except (ValueError, TypeError) as e:
                logger.error("Bad threshold for user %s: %s", row["telegram_id"], e)
        return User(
            telegram_id=row["telegram_id"],
            username=row["username"],
            language=row["language"] or "en",
            is_demo_mode=bool(row["is_demo_mode"]),
            threshold=threshold,
            session_epoch=row["session_epoch"] or 0,
            created_at=_parse_dt(row["created_at"]) or _now(),
            last_active=_parse_dt(row["last_active"]) or _now(),
        )

    async def get(self, telegram_id: int) -> Optional[User]:
        async with self.db.connect() as conn:
            async with conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
                row = await cur.fetchone()
        return self._row_to_user(row) if row else None

    async def save(self, user: User) -> None:
        async with self.db.connect() as conn:
            await conn.execute(
                """
                INSERT INTO users (telegram_id, username, language, is_demo_mode, threshold_json, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    language = excluded.language,
                    is_demo_mode = excluded.is_demo_mode,
                    threshold_json = excluded.threshold_json,
                    last_active = excluded.last_active
                """,
                (
                    user.telegram_id, user.username, user.language, int(user.is_demo_mode),
                    json.dumps(user.threshold.to_dict()), user.created_at.isoformat(), user.last_active.isoformat(),
                ),
            )
            await conn.commit()

    async def _update(self, telegram_id: int, column: str, value) -> None:
        async with self.db.connect() as conn:
            await conn.execute(f"UPDATE users SET {column} = ? WHERE telegram_id = ?", (value, telegram_id))
            await conn.commit()

    async def update_threshold(self, telegram_id: int, threshold: UserThreshold) -> None:
        await self._update(telegram_id, "threshold_json", json.dumps(threshold.to_dict()))

    async def set_demo_mode(self, telegram_id: int, enabled: bool) -> None:
        await self._update(telegram_id, "is_demo_mode", int(enabled))

    async def get_session_epoch(self, telegram_id: int) -> Optional[int]:
        async with self.db.connect() as conn:
            async with conn.execute("SELECT session_epoch FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
                row = await cur.fetchone()
        return row["session_epoch"] if row else None

    async def bump_session_epoch(self, telegram_id: int) -> int:
        async with self.db.connect() as conn:
            await conn.execute(
                "UPDATE users SET session_epoch = session_epoch + 1 WHERE telegram_id = ?", (telegram_id,)
            )
            await conn.commit()
        return await self.get_session_epoch(telegram_id) or 0

    async def ids_with_active_bindings(self) -> list[int]:
        async with self.db.connect() as conn:
            async with conn.execute(
                "SELECT DISTINCT telegram_id FROM bindings WHERE is_active = 1 ORDER BY telegram_id"
            ) as cur:
                return [row["telegram_id"] for row in await cur.fetchall()]

    async def demo_user_ids(self) -> list[int]:
        async with self.db.connect() as conn:
            async with conn.execute("SELECT telegram_id FROM users WHERE is_demo_mode = 1") as cur:
                return [row["telegram_id"] for row in await cur.fetchall()]


class BindingRepository:
    """Exchange credentials, encrypted with the owning user's data key."""

    def __init__(self, db: Database, keyring: UserKeyring):
        self.db = db
        self.keyring = keyring

    async def list_for_user(self, telegram_id: int) -> list[ExchangeBinding]:
        async with self.db.connect() as conn:
            async with conn.execute(
                "SELECT * FROM bindings WHERE telegram_id = ? ORDER BY id", (telegram_id,)
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                return []
            dek = await self.keyring.get(conn, telegram_id)

        bindings = []
        for row in rows:
            try:
                if dek is None:
                    raise InvalidToken
                api_key = decrypt_with(dek, row["api_key_encrypted"])
                api_secret = decrypt_with(dek, row["api_secret_encrypted"])
                wallet = decrypt_with(dek, row["wallet_address_encrypted"])
            except InvalidToken:
                logger.error(
                    "Binding %s/%s of user %s cannot be decrypted with that user's key; skipping",
                    row["venue"], row["network"], telegram_id,
                )
                continue
            bindings.append(ExchangeBinding(
                venue=row["venue"],
                network=row["network"] or MAINNET,
                binding_type=row["binding_type"],
                api_key=api_key,
                api_secret=api_secret,
                wallet_address=wallet,
                permissions=json.loads(row["permissions_json"] or "[]"),
                is_active=bool(row["is_active"]),
                created_at=_parse_dt(row["created_at"]) or _now(),
                last_used=_parse_dt(row["last_used"]),
            ))
        return bindings

    async def save(self, telegram_id: int, binding: ExchangeBinding) -> None:
        async with self.db.connect() as conn:
            dek = await self.keyring.get(conn, telegram_id, create=True)
            await conn.execute(
                """
                INSERT INTO bindings (
                    telegram_id, venue, network, binding_type, api_key_encrypted, api_secret_encrypted,
                    wallet_address_encrypted, permissions_json, is_active, created_at, last_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id, venue, network) DO UPDATE SET
                    binding_type = excluded.binding_type,
                    api_key_encrypted = excluded.api_key_encrypted,
                    api_secret_encrypted = excluded.api_secret_encrypted,
                    wallet_address_encrypted = excluded.wallet_address_encrypted,
                    permissions_json = excluded.permissions_json,
                    is_active = excluded.is_active,
                    last_used = excluded.last_used
                """,
                (
                    telegram_id, binding.venue, binding.network, binding.binding_type,
                    encrypt_with(dek, binding.api_key),
                    encrypt_with(dek, binding.api_secret),
                    encrypt_with(dek, binding.wallet_address),
                    json.dumps(binding.permissions),
                    int(binding.is_active),
                    binding.created_at.isoformat(),
                    binding.last_used.isoformat() if binding.last_used else None,
                ),
            )
            await conn.commit()

    async def delete(self, telegram_id: int, venue: str, network: Optional[str] = None) -> bool:
        query = "DELETE FROM bindings WHERE telegram_id = ? AND venue = ?"
        params: tuple = (telegram_id, venue)
        if network:
            query += " AND network = ?"
            params += (network,)
        async with self.db.connect() as conn:
            cur = await conn.execute(query, params)
            await conn.commit()
            return cur.rowcount > 0


class AuditRepository:
    """Protective-action audit trail, one row per step, always tagged with the owner."""

    def __init__(self, db: Database):
        self.db = db

    async def list_for_user(self, telegram_id: int, limit: int = 100) -> list[dict]:
        async with self.db.connect() as conn:
            async with conn.execute(
                "SELECT * FROM audit_log WHERE telegram_id = ? ORDER BY id DESC LIMIT ?", (telegram_id, limit)
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "timestamp": r["timestamp"],
                "action_id": r["action_id"],
                "action_type": r["action_type"],
                "symbol": r["symbol"],
                "venue": r["venue"],
                "status": r["status"],
                "amount_usd": r["amount_usd"],
                "details": json.loads(r["details_json"] or "{}"),
            }
            for r in reversed(rows)
        ]


class SqliteAuditSink:
    """Synchronous `AuditSink` so audit writes are durable and ordered before the call returns."""

    def __init__(self, db_path):
        self.db_path = str(db_path)

    def write(self, entry) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO audit_log (telegram_id, timestamp, action_id, action_type, symbol, venue, status,
                                       amount_usd, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.user_id, entry.timestamp.isoformat(), entry.action_id, entry.action_type.value,
                    entry.symbol, entry.venue, entry.status.value, entry.amount_usd,
                    json.dumps(entry.details, default=str),
                ),
            )


class LoginTokenRepository:
    """One-time dashboard login tokens. Only SHA-256 hashes are stored."""

    def __init__(self, db: Database):
        self.db = db

    async def create(self, telegram_id: int, token_hash: str, created_at: float, expires_at: float) -> None:
        async with self.db.connect() as conn:
            await conn.execute("DELETE FROM login_tokens WHERE expires_at < ?", (created_at,))
            await conn.execute(
                "INSERT INTO login_tokens (token_hash, telegram_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, telegram_id, created_at, expires_at),
            )
            await conn.commit()

    async def consume(self, token_hash: str, now: float) -> Optional[int]:
        """Mark the token used and return its owner, or None if unknown, expired or already used."""
        async with self.db.connect() as conn:
            cur = await conn.execute(
                "UPDATE login_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, token_hash, now),
            )
            if cur.rowcount != 1:
                await conn.commit()
                return None
            async with conn.execute(
                "SELECT telegram_id FROM login_tokens WHERE token_hash = ?", (token_hash,)
            ) as c:
                row = await c.fetchone()
            await conn.commit()
        return row["telegram_id"] if row else None

    async def revoke_all(self, telegram_id: int) -> None:
        async with self.db.connect() as conn:
            await conn.execute("DELETE FROM login_tokens WHERE telegram_id = ?", (telegram_id,))
            await conn.commit()
