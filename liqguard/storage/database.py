"""SQLite schema and connection helper."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

SCHEMA_VERSION = 6

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

-- PayPal subscriptions for LiqGuard Pro
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    subscription_id TEXT NOT NULL UNIQUE,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    payer_email TEXT,
    activated_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);

-- One-time Rescue Passes → 24h Rescue Mode (expires_at)
CREATE TABLE IF NOT EXISTS rescue_passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    remaining INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rescue_passes_user ON rescue_passes(telegram_id);

-- Muse daily usage (Taipei calendar day)
CREATE TABLE IF NOT EXISTS muse_usage (
    telegram_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, day)
);

-- Webhook events for idempotency
CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    resource_id TEXT,
    telegram_id INTEGER,
    payload_json TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_events_type ON payment_events(event_type);

-- Muse PayPal agent tool calls (create_order / create_subscription)
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    tool TEXT,
    product TEXT,
    source TEXT NOT NULL,
    resource_id TEXT,
    approve_url TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_user ON agent_actions(telegram_id, id);
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
