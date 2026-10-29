"""SQLite schema and connection helper."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    language TEXT DEFAULT 'en',
    language_explicit INTEGER NOT NULL DEFAULT 0,
    is_demo_mode INTEGER DEFAULT 0,
    threshold_json TEXT,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_active TEXT
);

CREATE TABLE IF NOT EXISTS user_keys (
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    wrapped_dek TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    venue TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT 'mainnet',
    binding_type TEXT NOT NULL,
    api_key_encrypted TEXT,
    api_secret_encrypted TEXT,
    wallet_address_encrypted TEXT,
    permissions_json TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT,
    last_used TEXT,
    UNIQUE(telegram_id, venue, network)
);
CREATE INDEX IF NOT EXISTS idx_bindings_user ON bindings(telegram_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    action_id TEXT,
    action_type TEXT,
    symbol TEXT,
    venue TEXT,
    status TEXT,
    amount_usd REAL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(telegram_id, id);

CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_login_tokens_user ON login_tokens(telegram_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA secure_delete = ON")
            yield db

    async def columns(self, db: aiosqlite.Connection, table: str) -> set[str]:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            return {row["name"] for row in await cur.fetchall()}

    def restrict_permissions(self) -> None:
        """The DB holds wrapped keys and ciphertext only, but keep it private anyway."""
        try:
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except OSError:
            pass
